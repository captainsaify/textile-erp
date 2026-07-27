"""Partner capital in and out -- docs/06_Accounting.md §8, commands in
docs/08_WhatsApp.md #capital / #withdraw.

Contributions and small withdrawals post immediately, exactly like
`expense`/`income`: business row + simplified ledger + journal + audit,
all in one transaction. Withdrawals at or above
`settings.capital_withdrawal_dual_approval_threshold` instead create a
*pending* row that moves no money at all until a second partner approves
-- see PartnerCapitalRepository.create_pending for why a pending request
must stay out of the balance chain.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Partner, PartnerCapital, User
from backend.models.enums import AccountCode, CapitalEntryType, LedgerEntryType
from backend.repositories.accounting_repository import (
    LedgerRepository,
    PartnerCapitalRepository,
    business_today,
)
from backend.repositories.party_repository import PartnerRepository
from backend.repositories.settings_repository import SettingsRepository
from backend.services.audit_service import AuditService
from backend.services.journal_service import JournalService

TWO_PLACES = decimal.Decimal("0.01")
ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class CapitalPosted:
    partner_name: str
    entry_type: CapitalEntryType
    amount: decimal.Decimal
    via: str
    new_balance: decimal.Decimal
    #: set when a withdrawal leaves the partner in deficit -- allowed,
    #: but never silently normal (docs/06_Accounting.md §13)
    negative_balance: bool


@dataclasses.dataclass(frozen=True)
class WithdrawalPending:
    request_id: uuid.UUID
    short_id: str
    partner_name: str
    amount: decimal.Decimal
    via: str
    threshold: decimal.Decimal
    #: (display_name, whatsapp_number) of partners who may approve
    approvers: list[tuple[str, str]]


def _validate_amount(raw: decimal.Decimal) -> decimal.Decimal:
    if raw <= ZERO:
        raise ValidationError("Amount must be greater than zero.")
    if raw != raw.quantize(TWO_PLACES):
        raise ValidationError("Amount can have at most 2 decimal places.")
    return raw.quantize(TWO_PLACES)


def short_id(request_id: uuid.UUID) -> str:
    return str(request_id)[:8]


class CapitalService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._capital = PartnerCapitalRepository(session)
        self._ledgers = LedgerRepository(session)
        self._partners = PartnerRepository(session)
        self._settings = SettingsRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def _resolve_partner(self, org_id: uuid.UUID, name: str) -> Partner:
        partner = await self._partners.get_by_display_name(org_id, name)
        if partner is None:
            raise NotFoundError("partner", name)
        return partner

    async def _approvers(
        self, org_id: uuid.UUID, exclude_partner_id: uuid.UUID
    ) -> list[tuple[str, str]]:
        """Other partners reachable on WhatsApp. A partner with no linked
        user (or no number) can't approve, so they aren't listed as
        someone the requester is waiting on."""
        from backend.models import User as UserModel

        rows = (
            await self._session.execute(
                select(Partner.display_name, UserModel.whatsapp_number)
                .join(UserModel, UserModel.id == Partner.user_id)
                .where(
                    Partner.org_id == org_id,
                    Partner.deleted_at.is_(None),
                    Partner.id != exclude_partner_id,
                    UserModel.deleted_at.is_(None),
                    UserModel.whatsapp_number.is_not(None),
                )
                .order_by(Partner.display_name)
            )
        ).all()
        return [(name, number) for name, number in rows]

    async def _post_entry(
        self,
        actor: User,
        partner: Partner,
        *,
        entry_type: CapitalEntryType,
        amount: decimal.Decimal,
        via: str,
        today: datetime.date,
        source_row: PartnerCapital | None = None,
    ) -> decimal.Decimal:
        """The money-moving half, shared by an immediate post and a
        just-approved one. Signed so a withdrawal is negative on both
        the capital row and the cash/bank ledger.

        Postings per docs/06_Accounting.md §3:
          contribution -> debit cash/bank, credit partner_capital
          withdrawal   -> debit partner_capital, credit cash/bank
        """
        org_id = actor.org_id
        is_withdrawal = entry_type is CapitalEntryType.WITHDRAWAL
        signed = -amount if is_withdrawal else amount
        ledger_account = AccountCode.CASH if via == "cash" else AccountCode.BANK

        if source_row is not None:
            capital_row = await self._capital.post_pending(
                source_row, approver_partner_id=partner.id
            )
        else:
            capital_row = await self._capital.append(
                org_id,
                partner.id,
                entry_type=entry_type,
                amount=signed,
                settled_via=via,
                entry_date=today,
                notes=None,
                created_by=actor.id,
            )

        await self._ledgers.append(
            org_id,
            via,
            entry_type=(
                LedgerEntryType.CAPITAL_OUT if is_withdrawal else LedgerEntryType.CAPITAL_IN
            ),
            amount=signed,
            source_type="partner_capital",
            source_id=capital_row.id,
            entry_date=today,
            notes=f"{entry_type.value}: {partner.display_name}",
            created_by=actor.id,
        )
        if is_withdrawal:
            debits = [(AccountCode.PARTNER_CAPITAL, amount)]
            credits = [(ledger_account, amount)]
        else:
            debits = [(ledger_account, amount)]
            credits = [(AccountCode.PARTNER_CAPITAL, amount)]
        await self._journal.post(
            org_id,
            entry_date=today,
            description=f"partner {entry_type.value}: {partner.display_name}",
            source_type="partner_capital",
            source_id=capital_row.id,
            created_by=actor.id,
            debits=debits,
            credits=credits,
        )
        await self._audit.record(
            org_id,
            actor.id,
            action=f"capital.{entry_type.value}",
            entity_type="partner_capital",
            entity_id=capital_row.id,
            after_state={
                "partner_id": str(partner.id),
                "amount": str(signed),
                "settled_via": via,
                "resulting_balance": str(capital_row.resulting_balance),
            },
        )
        return capital_row.resulting_balance

    async def record_contribution(
        self,
        actor: User,
        *,
        partner_name: str,
        amount: decimal.Decimal,
        via: str,
        whatsapp_message_id: str | None = None,
    ) -> CapitalPosted:
        amount = _validate_amount(amount)
        async with self._session.begin():
            today = await business_today(self._session, actor.org_id)
            partner = await self._resolve_partner(actor.org_id, partner_name)
            balance = await self._post_entry(
                actor,
                partner,
                entry_type=CapitalEntryType.CONTRIBUTION,
                amount=amount,
                via=via,
                today=today,
            )
        return CapitalPosted(
            partner_name=partner.display_name,
            entry_type=CapitalEntryType.CONTRIBUTION,
            amount=amount,
            via=via,
            new_balance=balance,
            negative_balance=balance < ZERO,
        )

    async def record_withdrawal(
        self,
        actor: User,
        *,
        partner_name: str,
        amount: decimal.Decimal,
        via: str,
        whatsapp_message_id: str | None = None,
    ) -> CapitalPosted | WithdrawalPending:
        """Below the threshold this posts immediately; at or above it, a
        pending request is created and nothing moves (§8)."""
        amount = _validate_amount(amount)
        async with self._session.begin():
            today = await business_today(self._session, actor.org_id)
            partner = await self._resolve_partner(actor.org_id, partner_name)
            threshold = await self._settings.withdrawal_dual_approval_threshold(actor.org_id)

            if amount < threshold:
                balance = await self._post_entry(
                    actor,
                    partner,
                    entry_type=CapitalEntryType.WITHDRAWAL,
                    amount=amount,
                    via=via,
                    today=today,
                )
                return CapitalPosted(
                    partner_name=partner.display_name,
                    entry_type=CapitalEntryType.WITHDRAWAL,
                    amount=amount,
                    via=via,
                    new_balance=balance,
                    negative_balance=balance < ZERO,
                )

            approvers = await self._approvers(actor.org_id, partner.id)
            if not approvers:
                raise ValidationError(
                    f"A withdrawal of ₹{amount} needs a second partner's approval, but no "
                    "other partner has a WhatsApp number registered. Add one, or withdraw "
                    f"less than ₹{threshold}."
                )
            pending = await self._capital.create_pending(
                actor.org_id,
                partner.id,
                amount=-amount,
                settled_via=via,
                entry_date=today,
                notes=f"awaiting approval, requested by {actor.full_name}",
                created_by=actor.id,
            )
            await self._audit.record(
                actor.org_id,
                actor.id,
                action="capital.withdrawal_requested",
                entity_type="partner_capital",
                entity_id=pending.id,
                after_state={
                    "partner_id": str(partner.id),
                    "amount": str(-amount),
                    "settled_via": via,
                    "status": "pending",
                },
                whatsapp_message_id=whatsapp_message_id,
            )
            return WithdrawalPending(
                request_id=pending.id,
                short_id=short_id(pending.id),
                partner_name=partner.display_name,
                amount=amount,
                via=via,
                threshold=threshold,
                approvers=approvers,
            )

    async def _resolve_request(self, org_id: uuid.UUID, reference: str) -> PartnerCapital:
        matches = await self._capital.find_pending_by_prefix(org_id, reference.strip())
        if not matches:
            raise NotFoundError("pending withdrawal", reference)
        if len(matches) > 1:
            raise ValidationError(
                f"'{reference}' matches {len(matches)} pending withdrawals — "
                "use more characters of the id."
            )
        return matches[0]

    async def _expired(self, row: PartnerCapital) -> bool:
        hours = await self._settings.withdrawal_approval_timeout_hours(row.org_id)
        age = datetime.datetime.now(datetime.UTC) - row.created_at
        return age > datetime.timedelta(hours=hours)

    async def approve_withdrawal(
        self, actor: User, reference: str, *, whatsapp_message_id: str | None = None
    ) -> CapitalPosted:
        async with self._session.begin():
            row = await self._resolve_request(actor.org_id, reference)
            approver = await self._partners.get_by_user_id(actor.org_id, actor.id)
            if approver is None:
                raise ValidationError("Only a partner can approve a capital withdrawal.")
            if approver.id == row.partner_id:
                # §8: the whole point of the second signature
                raise ValidationError(
                    "You can't approve your own withdrawal — it needs another partner."
                )
            if await self._expired(row):
                await self._capital.reject_pending(row)
                hours = await self._settings.withdrawal_approval_timeout_hours(actor.org_id)
                raise ValidationError(
                    f"That withdrawal request expired after {hours}h and has been cancelled. "
                    "Ask for it to be requested again."
                )

            requester = await self._session.get(Partner, row.partner_id)
            if requester is None:
                raise NotFoundError("partner", str(row.partner_id))
            today = await business_today(self._session, actor.org_id)
            amount = abs(row.amount)
            via = row.settled_via or "cash"
            balance = await self._post_entry(
                actor,
                requester,
                entry_type=CapitalEntryType.WITHDRAWAL,
                amount=amount,
                via=via,
                today=today,
                source_row=row,
            )
        return CapitalPosted(
            partner_name=requester.display_name,
            entry_type=CapitalEntryType.WITHDRAWAL,
            amount=amount,
            via=via,
            new_balance=balance,
            negative_balance=balance < ZERO,
        )

    async def reject_withdrawal(
        self, actor: User, reference: str, *, whatsapp_message_id: str | None = None
    ) -> tuple[str, decimal.Decimal]:
        async with self._session.begin():
            row = await self._resolve_request(actor.org_id, reference)
            approver = await self._partners.get_by_user_id(actor.org_id, actor.id)
            if approver is None:
                raise ValidationError("Only a partner can reject a capital withdrawal.")
            if approver.id == row.partner_id:
                raise ValidationError(
                    "You can't reject your own withdrawal — it needs another partner."
                )
            requester = await self._session.get(Partner, row.partner_id)
            if requester is None:
                raise NotFoundError("partner", str(row.partner_id))
            await self._capital.reject_pending(row)
            await self._audit.record(
                actor.org_id,
                actor.id,
                action="capital.withdrawal_rejected",
                entity_type="partner_capital",
                entity_id=row.id,
                after_state={"status": "rejected"},
                whatsapp_message_id=whatsapp_message_id,
            )
            return requester.display_name, abs(row.amount)
