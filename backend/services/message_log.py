"""Recording what we tried to send, and what came back.

A send that fails is an event about the business -- a partner did not
get their sheet -- but until now it was only a log line. Seventeen
messages failed overnight with Meta code 131047 and the only way to
find out was to read the container's stdout, which is to say: nobody
found out.

Three rules make this safe to call from the transport clients:

* **Its own session.** The log must survive when the work that
  triggered the send rolls back, and the send happens outside that
  transaction anyway.
* **It never raises.** A purchase that saved must not fail because the
  telemetry write did. Every failure here degrades to a log line, which
  is exactly where we started -- no worse.
* **It never blocks the send result.** The value the client returns is
  decided before this is called.
"""

from __future__ import annotations

import contextlib
import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.models import MessageLog

logger = get_logger(__name__)

#: Enough to recognise which message this was. Not a second copy of the
#: business record -- that already exists, in the table the message was
#: generated from.
PREVIEW_CHARS = 300


def meta_error(body: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Pull `(code, message)` out of a Graph API error envelope.

    Meta puts the useful part three levels down and moves it around
    between error shapes, so every lookup is defensive: a rejection we
    cannot parse still gets recorded, just without its code.
    """
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    detail = error.get("error_data")
    text = ""
    if isinstance(detail, dict):
        text = str(detail.get("details") or "")
    text = text or str(error.get("message") or "")
    return (None if code is None else str(code)), (text[:500] or None)


async def record(
    *,
    direction: str,
    transport: str,
    peer: str,
    kind: str,
    preview: str = "",
    ok: bool,
    http_status: int | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    attempts: int = 1,
) -> None:
    """Write one delivery row. Best effort, always."""
    try:
        from backend.core.db import get_session_factory

        factory = get_session_factory()
        async with factory() as session, session.begin():
            session.add(
                MessageLog(
                    direction=direction,
                    transport=transport,
                    peer=peer or "—",
                    kind=kind,
                    preview=preview[:PREVIEW_CHARS],
                    ok=ok,
                    http_status=http_status,
                    error_code=error_code,
                    error_detail=error_detail,
                    attempts=attempts,
                )
            )
    except Exception as exc:  # noqa: BLE001 -- telemetry must not break delivery
        logger.warning("message_log_write_failed", error=str(exc))


def fire_and_forget(**fields: Any) -> None:
    """Record without making the caller await it.

    Used where the send path is already returning: the row is worth
    having, but not worth another round-trip of latency on a message the
    user is waiting for. `contextlib.suppress` because there is no
    running loop in a synchronous test harness, and that is not an error
    worth reporting.
    """
    import asyncio

    with contextlib.suppress(RuntimeError):
        task = asyncio.get_running_loop().create_task(record(**fields))
        # Held so the garbage collector cannot cancel a task nobody
        # awaits -- the documented failure mode of bare create_task.
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


_PENDING: set[Any] = set()


def _window(since_hours: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=since_hours)


async def recent(
    session: AsyncSession, *, limit: int = 50, failed_only: bool = False
) -> list[MessageLog]:
    query: Select[Any] = select(MessageLog).order_by(MessageLog.created_at.desc()).limit(limit)
    if failed_only:
        query = query.where(MessageLog.ok.is_(False))
    return list((await session.execute(query)).scalars())


async def failure_summary(session: AsyncSession, *, since_hours: int = 24) -> dict[str, Any]:
    """What is broken right now, grouped by the code the far end gave.

    Grouped rather than listed because seventeen failures with one cause
    are one problem, and a screen that shows them as seventeen rows
    hides that.
    """
    since = _window(since_hours)
    total = (
        await session.execute(
            select(func.count()).select_from(MessageLog).where(MessageLog.created_at >= since)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(
                MessageLog.error_code,
                func.count().label("n"),
                func.max(MessageLog.error_detail),
                func.max(MessageLog.created_at),
            )
            .where(
                MessageLog.created_at >= since,
                MessageLog.ok.is_(False),
                MessageLog.direction == "out",
            )
            .group_by(MessageLog.error_code)
            .order_by(func.count().desc())
        )
    ).all()
    return {
        "window_hours": since_hours,
        "messages": int(total),
        "failed": sum(int(n) for _, n, _, _ in rows),
        "causes": [
            {
                "code": code or "—",
                "count": int(n),
                "detail": detail or "",
                "last_seen": last.isoformat(),
                "meaning": MEANINGS.get(str(code), ""),
            }
            for code, n, detail, last in rows
        ],
    }


#: The codes these books actually hit, in words. A number alone sends
#: the reader to Meta's documentation; the sentence tells them whether
#: it is their problem or ours.
MEANINGS = {
    "131047": (
        "more than 24 hours since that person last messaged us — Meta only "
        "allows a template message after that, not free text"
    ),
    "131026": "that number cannot receive WhatsApp messages",
    "131030": "not on the test number's allow-list of 5 recipients",
    "130472": "the recipient is in an experiment group that blocks this",
    "132000": "the template's placeholder count does not match what was sent",
    "133010": "the sending number is not registered",
    "100": "the request was malformed — ours to fix",
    "190": "the access token was rejected",
    "80007": "rate limit — too many messages too quickly",
}
