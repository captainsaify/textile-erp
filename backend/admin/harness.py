"""The safety net every mutating admin command runs inside.

docs/31_AdminCLI.md §3. The order matters and is not arbitrary:

    baseline reconciliation   what is *already* wrong, so a repair of a
                              broken book is not blocked by the breakage
                              it is repairing
    backup                    before anything opens, because it is the
                              only thing that survives a bug in this file
    one transaction           the work
    reconciliation again      inside the transaction, so it sees the
                              uncommitted result
    commit, or roll back      a regression means the command did not
                              happen

The reason this exists rather than trusting the service layer: during
development a repair script that went entirely through services replayed
cost history while ignoring zero-quantity movements, silently discarded
every rate correction across 28 products, and overstated stock by about
1.3 lakh. It was caught by a person who knew what one product cost.
"Goes through services" was true of that script too.
"""

from __future__ import annotations

import contextlib
import dataclasses
import decimal
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.admin import console
from backend.models import Organization, User
from backend.models.enums import UserRole
from backend.services.backup_service import BackupService
from backend.services.demo_service import DEMO_ORG_ID
from backend.services.reconciliation_service import Discrepancy, ReconciliationService


class AdminError(RuntimeError):
    """Anything the operator did wrong, or that the books refuse.

    Printed as a message, never as a traceback: a stack trace on a
    terminal at 3 a.m. buries the one sentence that says what to do."""


class ReconciliationRegressed(AdminError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("reconciliation regressed")
        self.problems = problems


class _Rollback(Exception):
    """Internal: unwinds the transaction without reporting an error.

    Used by --dry-run, which is a success that must not commit."""


@dataclasses.dataclass
class AdminContext:
    session: AsyncSession
    org: Organization
    actor: User
    dry_run: bool = False
    assume_yes: bool = False

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.id

    @property
    def is_demo(self) -> bool:
        return self.org.id == DEMO_ORG_ID


async def resolve_context(
    session: AsyncSession, *, demo: bool, dry_run: bool, assume_yes: bool
) -> AdminContext:
    """Pick the organisation and the person the change is attributed to.

    `audit_logs.actor_user_id` is NOT NULL and a real foreign key, so the
    CLI cannot invent a synthetic actor. It uses the owner, which is also
    the honest answer: someone with the box's SSH key and the owner's
    books is the owner."""
    target = DEMO_ORG_ID if demo else None
    stmt = select(Organization)
    if target is not None:
        stmt = stmt.where(Organization.id == target)
    else:
        stmt = stmt.where(Organization.id != DEMO_ORG_ID)
    org = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if org is None:
        raise AdminError(
            "no demo organisation exists yet -- send `demo` from WhatsApp once to create it."
            if demo
            else "no organisation found -- run `alembic upgrade head` first."
        )

    actor = (
        await session.execute(
            select(User)
            .where(
                User.org_id == org.id,
                User.role == UserRole.OWNER,
                User.deleted_at.is_(None),
            )
            .order_by(User.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if actor is None:
        raise AdminError(
            f"{org.name} has no owner to attribute changes to. "
            "Create one: python -m backend.cli create-user --role owner ..."
        )
    return AdminContext(
        session=session, org=org, actor=actor, dry_run=dry_run, assume_yes=assume_yes
    )


def _gap(d: Discrepancy) -> decimal.Decimal:
    try:
        return abs(decimal.Decimal(d.cached) - decimal.Decimal(d.replayed))
    except (decimal.InvalidOperation, ValueError):
        # Non-numeric subjects exist (a ledger name, say). Any change at
        # all counts as the whole gap, which errs toward refusing.
        return decimal.Decimal(0) if d.cached == d.replayed else decimal.Decimal(1)


async def _snapshot(ctx: AdminContext) -> dict[str, decimal.Decimal]:
    """Every discrepancy on both books, by subject, with its size."""
    service = ReconciliationService(ctx.session)
    found: dict[str, decimal.Decimal] = {}
    for outcome in (
        await service.check_inventory(ctx.org_id),
        await service.check_ledgers(ctx.org_id),
    ):
        for d in outcome.discrepancies:
            found[f"{outcome.kind}:{d.subject}"] = _gap(d)
    return found


def _regressions(
    before: dict[str, decimal.Decimal], after: dict[str, decimal.Decimal]
) -> list[str]:
    """A regression is a subject that became wrong, or got wronger.

    Comparing only "was it listed" would let a command double an
    existing mismatch and call it no change; comparing only the count
    would let one problem be swapped for another."""
    problems = []
    for subject, gap in sorted(after.items()):
        was = before.get(subject)
        if was is None:
            problems.append(f"{subject}: now off by {gap}")
        elif gap > was:
            problems.append(f"{subject}: was off by {was}, now off by {gap}")
    return problems


@contextlib.asynccontextmanager
async def guarded(ctx: AdminContext, *, what: str, backup: bool = True) -> AsyncIterator[None]:
    """Run a mutation so that it either leaves the books balanced or
    does not happen at all."""
    before = await _snapshot(ctx)
    if before:
        console.warn(f"{len(before)} pre-existing discrepancy(ies) -- not caused by this command")
        for subject in sorted(before):
            console.item(console.dim(f"{subject}: off by {before[subject]}"))

    if backup and not ctx.dry_run:
        record = await BackupService(ctx.session).create_backup(ctx.org_id)
        console.item(console.dim(f"backup: {Path(record.file_path).name}"))

    # The baseline snapshot ran SELECTs, and a bare SELECT autobegins a
    # transaction (HANDOFF.md §5) -- so `session.begin()` below raises
    # "a transaction is already begun" and every mutating command dies
    # before doing anything. Nothing above wrote through the session
    # (create_backup shells out to pg_dump and never touches it), so
    # releasing that read transaction costs nothing, and it is what
    # makes the real one ownable and therefore roll-back-able.
    #
    # This was written the obvious way first and shipped: the pure-
    # function tests over `_regressions` all passed, because none of
    # them ever entered `guarded`. It failed on the first real command.
    #
    # Released with commit() rather than rollback() even though nothing
    # was written. rollback() expires every loaded instance
    # unconditionally, so the next attribute touch re-queries -- and if
    # that touch happens inside a flush, it raises MissingGreenlet
    # instead of doing IO. commit() honours expire_on_commit=False,
    # which both the app's session factory and the tests' set, so the
    # identity map survives. On a read-only transaction the two are
    # otherwise identical.
    if ctx.session.in_transaction():
        await ctx.session.commit()

    committed = False
    try:
        async with ctx.session.begin():
            yield
            after = await _snapshot(ctx)
            problems = _regressions(before, after)
            if problems:
                raise ReconciliationRegressed(problems)
            if ctx.dry_run:
                raise _Rollback
            committed = True
    except _Rollback:
        console.say()
        console.warn(f"--dry-run: {what} was NOT saved.")
        return
    except ReconciliationRegressed as exc:
        console.say()
        for problem in exc.problems:
            console.bad(problem)
        console.bad(console.bold("ROLLED BACK -- nothing was changed."))
        raise

    if committed:
        console.say()
        console.ok(f"{what} -- saved, books balance.")
