"""Saving what was typed before it is worth keeping -- docs/33_Drafts.md.

The rule that shapes everything here: a draft is a *saved form*, never a
business record. It can hold a supplier who does not exist, a code that
resolves to nothing, a total that does not add up. So nothing in this
module writes to the books, and turning a draft into a record means
sending it through the ordinary create endpoint, which validates it the
same way it validates anything typed fresh.

That is also why deleting a draft is a hard delete and needs no audit
row. The no-hard-delete rule protects business data; a draft is not
business data, and keeping every abandoned keystroke forever would make
the list useless at exactly the moment someone needs it.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import joined_transaction
from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Draft, User

ZERO = decimal.Decimal("0")

#: Abandoned drafts are noise, and noise is what stops a list being
#: read. Long enough to cover a holiday, short enough that the page
#: still means "things I am part-way through".
STALE_AFTER_DAYS = 30

KINDS = ("purchase", "sale")


@dataclasses.dataclass(frozen=True)
class DraftSummary:
    id: uuid.UUID
    client_key: str
    kind: str
    label: str
    line_count: int
    ready: bool
    total: str
    updated_at: datetime.datetime
    author: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "key": self.client_key,
            "kind": self.kind,
            "label": self.label,
            "lines": self.line_count,
            "ready": self.ready,
            "total": self.total,
            "updated_at": self.updated_at.isoformat(),
            "author": self.author,
        }


def _money(value: Any) -> decimal.Decimal:
    try:
        return decimal.Decimal(str(value or "0"))
    except (decimal.InvalidOperation, ValueError):
        return ZERO


def describe(payload: dict[str, Any]) -> tuple[str, int, bool, decimal.Decimal]:
    """What the list shows, worked out from the payload alone.

    Never resolves a product or a party: a draft must stay readable
    after the code it names has been merged away, and a list that hits
    the catalogue once per row is a list nobody waits for.
    """
    rows = [row for row in (payload.get("lines") or []) if isinstance(row, dict)]
    filled = [row for row in rows if str(row.get("code") or "").strip()]
    party = str(payload.get("party") or "").strip()

    total = ZERO
    for row in filled:
        total += _money(row.get("qty")) * _money(row.get("rate"))
    total += _money(payload.get("freight"))
    total -= _money(payload.get("discount"))
    for value in (payload.get("charges") or {}).values():
        total += _money(value)

    label = party or "(no party yet)"
    if payload.get("invoice"):
        label = f"{label} · {payload['invoice']}"
    # "Ready" is advisory. The create endpoint validates for itself; this
    # only decides whether the list offers a one-click record.
    ready = bool(party) and bool(filled) and all(_money(r.get("qty")) > ZERO for r in filled)
    return label, len(filled), ready, total


class DraftService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self, actor: User, *, key: str, kind: str, payload: dict[str, Any]
    ) -> DraftSummary:
        """Create or update the draft this form owns.

        Keyed by what the form minted when it opened, so a hundred
        autosaves are one row rather than a hundred near-identical ones.
        """
        if kind not in KINDS:
            raise ValidationError(f"A draft is a purchase or a sale, not {kind!r}.")
        org_id = actor.org_id
        label, lines, ready, total = describe(payload)
        payload = {**payload, "_total": str(total)}

        async with joined_transaction(self._session):
            existing = (
                await self._session.execute(
                    select(Draft).where(
                        Draft.org_id == org_id,
                        Draft.client_key == key,
                        Draft.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = Draft(
                    org_id=org_id,
                    client_key=key,
                    kind=kind,
                    created_by=actor.id,
                )
                self._session.add(existing)
            existing.kind = kind
            existing.payload = payload
            existing.label = label
            existing.line_count = lines
            existing.ready = ready
            existing.updated_at = datetime.datetime.now(datetime.UTC)
            await self._session.flush()
            return await self._summary(existing)

    async def list(self, org_id: uuid.UUID) -> list[DraftSummary]:
        """Most recently touched first -- the one being worked on now is
        the one being looked for."""
        from backend.models import User as UserModel

        rows = (
            await self._session.execute(
                select(Draft, UserModel.full_name)
                .join(UserModel, UserModel.id == Draft.created_by, isouter=True)
                .where(Draft.org_id == org_id, Draft.deleted_at.is_(None))
                .order_by(Draft.updated_at.desc())
            )
        ).all()
        return [
            DraftSummary(
                id=draft.id,
                client_key=draft.client_key,
                kind=draft.kind,
                label=draft.label,
                line_count=draft.line_count,
                ready=draft.ready,
                total=str(_money((draft.payload or {}).get("_total"))),
                updated_at=draft.updated_at,
                author=author or "—",
            )
            for draft, author in rows
        ]

    async def get(self, org_id: uuid.UUID, key: str) -> dict[str, Any]:
        draft = await self._row(org_id, key)
        return {
            "key": draft.client_key,
            "kind": draft.kind,
            "payload": draft.payload or {},
            "updated_at": draft.updated_at.isoformat(),
        }

    async def delete(self, org_id: uuid.UUID, key: str) -> str:
        """Gone, not soft-deleted. A draft is not business data, and the
        no-hard-delete rule exists to protect business data."""
        draft = await self._row(org_id, key)
        label = draft.label
        async with joined_transaction(self._session):
            await self._session.execute(delete(Draft).where(Draft.id == draft.id))
        return label

    async def purge_stale(self, org_id: uuid.UUID) -> int:
        """Drop drafts nobody has touched in a month."""
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=STALE_AFTER_DAYS)
        async with joined_transaction(self._session):
            stale = list(
                (
                    await self._session.execute(
                        select(Draft.id).where(Draft.org_id == org_id, Draft.updated_at < cutoff)
                    )
                ).scalars()
            )
            if stale:
                await self._session.execute(delete(Draft).where(Draft.id.in_(stale)))
        return len(stale)

    async def _row(self, org_id: uuid.UUID, key: str) -> Draft:
        draft = (
            await self._session.execute(
                select(Draft).where(
                    Draft.org_id == org_id,
                    Draft.client_key == key,
                    Draft.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if draft is None:
            raise NotFoundError("draft", key)
        return draft

    async def _summary(self, draft: Draft) -> DraftSummary:
        from backend.models import User as UserModel

        author = (
            await self._session.execute(
                select(UserModel.full_name).where(UserModel.id == draft.created_by)
            )
        ).scalar_one_or_none()
        return DraftSummary(
            id=draft.id,
            client_key=draft.client_key,
            kind=draft.kind,
            label=draft.label,
            line_count=draft.line_count,
            ready=draft.ready,
            total=str(_money((draft.payload or {}).get("_total"))),
            updated_at=draft.updated_at,
            author=author or "—",
        )
