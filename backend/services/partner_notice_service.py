"""Telling the other partners what you just did -- docs/22 §7.

Three people run this business and any of them can record a purchase, a
sale or a payment from their own phone. The one who typed it sees the
confirmation and the sheet; until now the other two saw nothing, and
found out when someone happened to open the dashboard.

So: **every recorded transaction reaches the partners who did not record
it, with the same sheet the person who did got.** Not a summary at the
end of the day -- a sale that shouldn't have happened is worth hearing
about while it can still be undone.

Fed from `audit_logs`, for the reasons
[broadcast_service][backend.services.broadcast_service] already argues:
only committed facts, nothing waits on the send, and one list rather
than a call added to thirty services by hand. It keeps its own
watermark, so it and the group sweep cannot swallow each other's
activity.

Who hears it is **owners with a WhatsApp number, minus the actor**. Not
`partners`: that table is the capital-accounting entity, and the
question here is which people to tell. An owner without a partner row
still runs the business; a partner row without a linked user has no
phone to reach.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.models import AuditLog, User
from backend.models.enums import UserRole
from backend.services.broadcast_service import fmt_money_safe

logger = get_logger(__name__)

#: Where this sweep got to. Separate from the group broadcast's own
#: watermark on purpose -- see the module docstring.
WATERMARK_KEY = "partner_notice_watermark"

#: What the partners hear about, keyed by the action actually written to
#: `audit_logs`. Checked against the live table rather than written from
#: memory: the group-broadcast map guessed at names like
#: `sale.confirmed` and `expense.recorded`, which this system has never
#: written, so it would have announced almost nothing.
#:
#: `product.created` and `supplier.created` are deliberately absent: one
#: photographed sheet creates 26 products as part of a purchase that is
#: itself announced, and 26 messages would train people to ignore this.
NOTIFIABLE: dict[str, str] = {
    "purchase.confirmed": "🧾 Purchase recorded",
    "purchase.confirmed.undone": "↩️ Purchase reversed",
    "purchase.rate_corrected": "✏️ Bill rate corrected",
    "purchase.receipt_corrected": "📦 Receipt corrected",
    "purchase.returned": "↩️ Returned to supplier",
    "sale.created": "💰 Sale recorded",
    "sale.created.undone": "↩️ Sale reversed",
    "sale.returned": "↩️ Customer return",
    "payment.paid": "💸 Paid a supplier",
    "payment.paid.undone": "↩️ Payment reversed",
    "payment.received": "💵 Received from a customer",
    "payment.received.undone": "↩️ Receipt reversed",
    "payment.reversed": "↩️ Payment reversed",
    "expense.created": "🧮 Expense",
    "expense.created.undone": "↩️ Expense reversed",
    "expense.reversed": "↩️ Expense reversed",
    "income.created": "🪙 Other income",
    "capital.contribution": "🏦 Capital in",
    "capital.withdrawal": "🏦 Capital out",
    "capital.withdrawal_requested": "🔒 Withdrawal awaiting approval",
    "capital.withdrawal_rejected": "🚫 Withdrawal rejected",
    "settings.updated": "⚙️ Setting changed",
}

#: Which of those carry a sheet, and how to build it. A correction is in
#: here as much as the original: the whole point is that a bill whose
#: rate changed reaches the other two as the *corrected* bill, not as a
#: note that something changed on a document they still hold the old
#: copy of.
_PURCHASE_ACTIONS = {
    "purchase.confirmed",
    "purchase.rate_corrected",
    "purchase.returned",
}
_LINE_ACTIONS = {"purchase.receipt_corrected"}
_SALE_ACTIONS = {"sale.created", "sale.returned"}
_PAYMENT_ACTIONS = {"payment.paid", "payment.received"}

MONEY_KEYS = ("amount", "grand_total", "total")
DETAIL_KEYS = ("invoice_no", "name", "code", "reference", "category", "partner")


@dataclasses.dataclass(frozen=True)
class DocumentRef:
    kind: str  # purchase | sale | payment
    reference: str


@dataclasses.dataclass(frozen=True)
class Notice:
    at: datetime.datetime
    actor_user_id: uuid.UUID | None
    body: str
    document: DocumentRef | None


@dataclasses.dataclass(frozen=True)
class Recipient:
    user_id: uuid.UUID
    name: str
    number: str


def headline(entry: AuditLog, actor: str | None) -> str:
    """One line a partner can read without opening anything."""
    label = NOTIFIABLE.get(entry.action, entry.action)
    state = entry.after_state or entry.before_state or {}

    parts: list[str] = []
    for key in MONEY_KEYS:
        if state.get(key):
            parts.append(fmt_money_safe(state[key]))
            break
    for key in DETAIL_KEYS:
        if state.get(key):
            parts.append(str(state[key]))
            break
    if state.get("via"):
        parts.append(f"({state['via']})")

    detail = " · ".join(parts)
    who = f" — by {actor}" if actor else ""
    return f"{label}: {detail}{who}" if detail else f"{label}{who}"


async def _document_for(session: AsyncSession, entry: AuditLog) -> DocumentRef | None:
    if entry.action in _PURCHASE_ACTIONS and entry.entity_id is not None:
        return DocumentRef("purchase", str(entry.entity_id))
    if entry.action in _SALE_ACTIONS and entry.entity_id is not None:
        return DocumentRef("sale", str(entry.entity_id))
    if entry.action in _PAYMENT_ACTIONS:
        # A payment's sheet is keyed by its own audit id, which is what
        # `undo payment` takes and what the confirmation prints.
        return DocumentRef("payment", str(entry.id)[:8])
    if entry.action in _LINE_ACTIONS and entry.entity_id is not None:
        # The audit row points at the *line* that was corrected; the
        # sheet is the bill it sits on.
        from backend.models import PurchaseLine

        line = await session.get(PurchaseLine, entry.entity_id)
        if line is not None:
            return DocumentRef("purchase", str(line.purchase_header_id))
    return None


async def pending_notices(
    session: AsyncSession,
    org_id: uuid.UUID,
    since: datetime.datetime,
    *,
    limit: int = 50,
) -> tuple[list[Notice], datetime.datetime | None]:
    """Notifiable activity since `since`, newest timestamp included so
    the watermark only advances over what was actually read.

    Not collapsed the way a group post is. A group channel is skimmed
    and twenty lines bury each other; this is one message per
    transaction *because* each one carries that transaction's sheet, and
    a sheet with no transaction beside it is unreadable.
    """
    stmt = (
        select(AuditLog, User.full_name)
        .join(User, User.id == AuditLog.actor_user_id, isouter=True)
        .where(
            AuditLog.org_id == org_id,
            AuditLog.created_at > since,
            AuditLog.action.in_(tuple(NOTIFIABLE)),
        )
        .order_by(AuditLog.created_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return [], None

    notices = [
        Notice(
            at=entry.created_at,
            actor_user_id=entry.actor_user_id,
            body=headline(entry, actor),
            document=await _document_for(session, entry),
        )
        for entry, actor in rows
    ]
    return notices, max(notice.at for notice in notices)


async def recipients(
    session: AsyncSession, org_id: uuid.UUID, *, exclude_user_id: uuid.UUID | None
) -> list[Recipient]:
    """Everyone who should hear it, except whoever did it.

    Excluding the actor matters more than it looks: they already have
    the confirmation and the sheet in their own chat, and a duplicate
    arriving a minute later reads as a second transaction.
    """
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.whatsapp_number).where(
                User.org_id == org_id,
                User.role == UserRole.OWNER,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                User.whatsapp_number.is_not(None),
            )
        )
    ).all()
    return [
        Recipient(user_id=row[0], name=row[1], number=row[2])
        for row in rows
        if row[0] != exclude_user_id
    ]


def caption_for(notice: Notice) -> str:
    """What rides with the sheet. The headline itself: a file arriving
    with no words is a document nobody knows what to do with."""
    return notice.body


__all__ = [
    "NOTIFIABLE",
    "WATERMARK_KEY",
    "DocumentRef",
    "Notice",
    "Recipient",
    "caption_for",
    "headline",
    "pending_notices",
    "recipients",
]
