"""Which number reaches which person.

A WhatsApp number is the login. Everything the system does about
identity — who may run a command, whose name goes on an audit row, who
gets the partner notice — comes from matching the sender's number to a
`users` row, and nothing else. So when a partner changes SIM, they do
not have a new phone: they have no account at all, and the system's
correct response to an unrecognised number is silence.

That happened here. Firoz moved to 7000087329 and his messages went
nowhere, visibly doing nothing, for reasons that were only findable in a
log line saying `unauthorized_sender`.

Re-linking is a single column on a single row, which is exactly why it
is worth having a command for: the operation is trivial and the way to
get it wrong is not. Two users cannot hold one number (a partial unique
index says so), the old holder's session has to go or it will answer
mid-conversation as the wrong person, and the whole thing has to be
reversible like everything else here.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.core.security import normalize_whatsapp_number
from backend.models import AuditLog, User, WhatsappSession
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.reversal_service import ReversalService


class ContactAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def contacts(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        """Everyone who can reach the system, and when they last did.

        The last-seen column is the one that answers the question people
        actually arrive with — "is his number working?" — because a
        number that has never been seen is either wrong or unused, and
        the two look identical from a list of names.
        """
        users = list(
            (
                await self._session.execute(
                    select(User)
                    .where(User.org_id == org_id, User.deleted_at.is_(None))
                    .order_by(User.full_name)
                )
            ).scalars()
        )
        seen: dict[uuid.UUID, datetime.datetime] = {
            actor: when
            for actor, when in (
                await self._session.execute(
                    select(AuditLog.actor_user_id, func.max(AuditLog.created_at))
                    .where(AuditLog.org_id == org_id, AuditLog.channel == "whatsapp")
                    .group_by(AuditLog.actor_user_id)
                )
            ).all()
        }
        return [
            {
                "id": str(user.id),
                "name": user.full_name,
                "role": user.role.value,
                "whatsapp_number": user.whatsapp_number or "",
                "email": user.email or "",
                "active": user.is_active,
                "last_seen": (seen[user.id].isoformat() if seen.get(user.id) is not None else None),
            }
            for user in users
        ]

    async def _user(self, org_id: uuid.UUID, name: str) -> User:
        wanted = " ".join(name.split()).casefold()
        rows = list(
            (
                await self._session.execute(
                    select(User).where(User.org_id == org_id, User.deleted_at.is_(None))
                )
            ).scalars()
        )
        exact = [u for u in rows if " ".join(u.full_name.split()).casefold() == wanted]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValidationError(f"{len(exact)} people are called {name!r}")
        near = [u for u in rows if wanted in " ".join(u.full_name.split()).casefold()]
        if near:
            raise ValidationError(
                f"no one is named exactly {name!r}. Did you mean "
                + ", ".join(sorted(u.full_name for u in near))
                + "?"
            )
        raise NotFoundError("user", name)

    async def relink(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        number: str,
        to_name: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Point a WhatsApp number at a person.

        If someone else holds it, they lose it — which is the ordinary
        case, not the exception: a number moves *between* people far more
        often than it appears from nowhere.
        """
        wanted = normalize_whatsapp_number(number)
        if not wanted:
            raise ValidationError(f"{number!r} is not a usable WhatsApp number")
        target = await self._user(org_id, to_name)

        holder = (
            (
                await self._session.execute(
                    select(User).where(User.whatsapp_number == wanted, User.deleted_at.is_(None))
                )
            )
            .scalars()
            .first()
        )
        if holder is not None and holder.id == target.id:
            raise ValidationError(f"{wanted} already reaches {target.full_name}")
        if holder is not None and holder.email is None:
            # `login_method` on `users` requires a number or an email.
            # Taking the number from someone who has neither leaves a row
            # that cannot sign in by any route, so the database refuses
            # it -- better said here, in words, than as an IntegrityError.
            raise ValidationError(
                f"{wanted} is {holder.full_name}'s only way to sign in — "
                "give them an email address first, or remove them"
            )

        previous = target.whatsapp_number
        # Read before the guard: a rolled-back dry run expires every
        # loaded instance, and touching one afterwards re-queries -- which
        # outside the greenlet raises MissingGreenlet rather than doing
        # IO. These are plain strings; nothing below changes them.
        target_name, holder_name = target.full_name, holder.full_name if holder else ""
        moved: list[dict[str, Any]] = [
            {
                "table": "users",
                "id": str(target.id),
                "column": "whatsapp_number",
                "from": previous,
                "to": wanted,
            }
        ]
        if holder is not None:
            moved.append(
                {
                    "table": "users",
                    "id": str(holder.id),
                    "column": "whatsapp_number",
                    "from": wanted,
                    "to": None,
                }
            )

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            manifest = await ReversalService(self._session).record(
                org_id,
                actor,
                operation="relink_contact",
                subject=f"{wanted} → {target.full_name}",
                moved=moved,
            )
            # The old holder is cleared first. Both writes land in one
            # transaction, but the unique index is checked per statement,
            # so setting the new one first would collide with a row that
            # is about to be emptied.
            if holder is not None:
                holder.whatsapp_number = None
                await self._session.flush()
                report.note(f"taken from {holder.full_name}")
                await self._drop_session(org_id, holder.id, report)
            target.whatsapp_number = wanted
            await self._session.flush()
            report.note(f"{wanted} now reaches {target.full_name}")
            if previous:
                report.note(f"{target.full_name} previously used {previous}")
            # A half-finished wizard belongs to the conversation, not to
            # the number. Left in place, the next message from this phone
            # would be read as the next answer in someone else's flow.
            await self._drop_session(org_id, target.id, report)

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="contact.relinked",
                entity_type="users",
                entity_id=target.id,
                before_state={"number": previous, "held_by": holder.full_name if holder else None},
                after_state={"number": wanted, "manifest": str(manifest.id)},
                channel="cli",
            )
            reversal = str(manifest.id)[:8]

        return {
            "number": wanted,
            "user": target_name,
            "previous": previous or "",
            "taken_from": holder_name,
            "committed": report.committed,
            "notes": report.notes,
            "reversal": reversal,
        }

    async def _drop_session(self, org_id: uuid.UUID, user_id: uuid.UUID, report: Any) -> None:
        row = (
            (
                await self._session.execute(
                    select(WhatsappSession).where(
                        WhatsappSession.org_id == org_id, WhatsappSession.user_id == user_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return
        await self._session.delete(row)
        await self._session.flush()
        report.note("an unfinished conversation was cleared")

    async def unlink(
        self, org_id: uuid.UUID, actor: User, *, name: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Take a number away without giving it to anyone.

        For a SIM that is gone rather than moved. Separate from `relink`
        because "9977250571 is retired" and "9977250571 is now Firoz's"
        are different statements and only one of them is true.
        """
        target = await self._user(org_id, name)
        if not target.whatsapp_number:
            raise ValidationError(f"{target.full_name} has no number linked")
        if target.email is None:
            raise ValidationError(
                f"that number is {target.full_name}'s only way to sign in — "
                "give them an email address first, or remove them"
            )
        previous = target.whatsapp_number
        target_name = target.full_name  # read before the guard -- see `relink`

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            manifest = await ReversalService(self._session).record(
                org_id,
                actor,
                operation="relink_contact",
                subject=f"{previous} unlinked from {target_name}",
                moved=[
                    {
                        "table": "users",
                        "id": str(target.id),
                        "column": "whatsapp_number",
                        "from": previous,
                        "to": None,
                    }
                ],
            )
            target.whatsapp_number = None
            target.updated_at = datetime.datetime.now(datetime.UTC)
            await self._session.flush()
            await self._drop_session(org_id, target.id, report)
            report.note(f"{previous} no longer reaches anyone")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="contact.unlinked",
                entity_type="users",
                entity_id=target.id,
                before_state={"number": previous},
                after_state={"manifest": str(manifest.id)},
                channel="cli",
            )
            reversal = str(manifest.id)[:8]

        return {
            "number": previous,
            "user": target_name,
            "committed": report.committed,
            "notes": report.notes,
            "reversal": reversal,
        }
