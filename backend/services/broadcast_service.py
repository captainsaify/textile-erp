"""What gets told to the group, and when -- docs/22_GroupBroadcast.md.

Fed from `audit_logs` rather than from each command, for three reasons
that all matter:

1. **Only committed facts.** The audit row exists because the
   transaction succeeded. Broadcasting from inside a command could
   announce a purchase that then rolled back -- a message you cannot
   take back, about something that never happened.
2. **Nothing waits on the relay.** whatsapp-web.js is the unofficial,
   fragile half of this system. A partner's confirmation must never sit
   behind it.
3. **One list, not thirty call sites.** Every mutation already writes an
   audit row (`CLAUDE.md` rule 3), so what is broadcast is a filter over
   that, not a line added to every service by hand.

Bursts are collapsed. Photographing one purchase sheet creates 26
products; twenty-six "product created" messages would bury the one that
matters -- the purchase itself.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.formatting import fmt_money
from backend.core.logging import get_logger
from backend.models import AuditLog, User

logger = get_logger(__name__)

#: Key under which the last-broadcast timestamp lives in `settings`.
WATERMARK_KEY = "group_broadcast_watermark"

#: What the partners asked to hear about. Deliberately not "everything":
#: `product.created` is excluded because it arrives 26-at-a-time as part
#: of a purchase that is itself announced, and the noise would train
#: people to ignore the channel.
BROADCAST_ACTIONS: dict[str, str] = {
    "purchase.confirmed": "🧾 Purchase recorded",
    "purchase.undone": "↩️ Purchase reversed",
    "sale.confirmed": "💰 Sale recorded",
    "sale.undone": "↩️ Sale reversed",
    "payment.paid": "💸 Paid a supplier",
    "payment.received": "💵 Received from a customer",
    "expense.recorded": "🧮 Expense",
    "income.recorded": "🪙 Other income",
    "capital.contributed": "🏦 Capital in",
    "capital.withdrawn": "🏦 Capital out",
    "withdrawal.requested": "🔒 Withdrawal awaiting approval",
    "withdrawal.approved": "✅ Withdrawal approved",
    "withdrawal.rejected": "🚫 Withdrawal rejected",
    "supplier.created": "🏭 New supplier",
    "customer.created": "🧍 New customer",
    "product.brand_assigned": "🏷️ Products re-branded",
    "entity.edited": "✏️ Record edited",
    "entity.deleted": "🗑️ Record deleted",
    "backup.created": "💾 Backup taken",
    "backup.restored": "♻️ Backup restored",
}

#: Above this many of the same action in one sweep, say how many rather
#: than listing them.
COLLAPSE_AT = 3

MONEY_KEYS = ("amount", "grand_total", "total")


@dataclasses.dataclass(frozen=True)
class BroadcastLine:
    at: datetime.datetime
    text: str


def describe(entry: AuditLog, actor: str | None) -> str:
    """One line a person can read without knowing the schema."""
    label = BROADCAST_ACTIONS.get(entry.action, entry.action)
    state: dict[str, Any] = entry.after_state or entry.before_state or {}

    parts: list[str] = []
    for key in MONEY_KEYS:
        if state.get(key):
            parts.append(fmt_money_safe(state[key]))
            break
    for key in ("name", "code", "invoice_no", "reference", "category", "partner"):
        if state.get(key):
            parts.append(str(state[key]))
            break
    if state.get("via"):
        parts.append(f"({state['via']})")

    detail = " · ".join(parts)
    who = f" — {actor}" if actor else ""
    return f"{label}: {detail}{who}" if detail else f"{label}{who}"


def fmt_money_safe(raw: Any) -> str:
    import decimal

    try:
        return fmt_money(decimal.Decimal(str(raw)))
    except (decimal.InvalidOperation, TypeError, ValueError):
        return str(raw)


async def pending_lines(
    session: AsyncSession, org_id: uuid.UUID, since: datetime.datetime
) -> tuple[list[str], datetime.datetime | None]:
    """Broadcastable activity since `since`, collapsed, plus the newest
    timestamp seen so the watermark only advances over what was read."""
    stmt = (
        select(AuditLog, User.full_name)
        .join(User, User.id == AuditLog.actor_user_id, isouter=True)
        .where(
            AuditLog.org_id == org_id,
            AuditLog.created_at > since,
            AuditLog.action.in_(tuple(BROADCAST_ACTIONS)),
        )
        .order_by(AuditLog.created_at)
        .limit(200)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return [], None

    newest = max(entry.created_at for entry, _ in rows)

    grouped: dict[str, list[str]] = {}
    for entry, actor in rows:
        grouped.setdefault(entry.action, []).append(describe(entry, actor))

    lines: list[str] = []
    for action, described in grouped.items():
        if len(described) > COLLAPSE_AT:
            label = BROADCAST_ACTIONS.get(action, action)
            lines.append(f"{label}: {len(described)} entries")
            continue
        lines.extend(described)
    return lines, newest


async def read_watermark(
    session: AsyncSession, org_id: uuid.UUID, key: str = WATERMARK_KEY
) -> datetime.datetime:
    """Where the last sweep got to.

    A missing watermark means "start from now", not "replay everything":
    switching broadcasting on should not dump the entire history into
    the group.

    `key` is a parameter because a second sweep reads the same audit log
    for a different audience (the partner fan-out,
    docs/22_GroupBroadcast.md §7) and must keep its own place in it --
    one shared watermark would let whichever swept first swallow the
    other's activity.
    """
    from backend.models import Setting

    row = (
        await session.execute(select(Setting).where(Setting.org_id == org_id, Setting.key == key))
    ).scalar_one_or_none()
    if row is None or not row.value:
        return datetime.datetime.now(datetime.UTC)
    try:
        return datetime.datetime.fromisoformat(str(row.value))
    except ValueError:
        logger.warning("broadcast_watermark_unreadable", key=key, value=str(row.value))
        return datetime.datetime.now(datetime.UTC)


async def write_watermark(
    session: AsyncSession,
    org_id: uuid.UUID,
    moment: datetime.datetime,
    key: str = WATERMARK_KEY,
) -> None:
    from backend.models import Setting

    row = (
        await session.execute(select(Setting).where(Setting.org_id == org_id, Setting.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(Setting(org_id=org_id, key=key, value=moment.isoformat()))
    else:
        row.value = moment.isoformat()
