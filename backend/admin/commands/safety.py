"""`erp check`, `history`, `backup`, `restore`.

None of these repair anything. They are what you run before deciding to,
and after wishing you hadn't.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from backend.admin import console, resolve
from backend.admin.app import cli, confirm, run
from backend.admin.harness import AdminContext, AdminError
from backend.models import AuditLog, User
from backend.services.backup_service import BackupService
from backend.services.demo_service import DEMO_ORG_ID
from backend.services.reconciliation_service import ReconciliationService


@cli.command("check")
def check() -> None:
    """Do the books balance? Runs on both the real business and the demo.

    This is the same check every mutating command runs against itself
    before it is allowed to commit."""

    async def action(ctx: AdminContext) -> None:
        failed = False
        for org_id, label in ((ctx.org_id, ctx.org.name), (DEMO_ORG_ID, "demo")):
            if org_id == DEMO_ORG_ID and ctx.is_demo:
                continue
            service = ReconciliationService(ctx.session)
            console.head(label)
            for outcome in (
                await service.check_inventory(org_id),
                await service.check_ledgers(org_id),
            ):
                if outcome.ok:
                    console.ok(f"{outcome.kind}: {outcome.checked} checked, balanced")
                else:
                    failed = True
                    console.bad(f"{outcome.kind}: {len(outcome.discrepancies)} discrepancy(ies)")
                    for d in outcome.discrepancies:
                        console.item(
                            f"{d.subject}: recorded {d.cached}, movements say {d.replayed}"
                        )
        if failed:
            console.say()
            console.warn("`erp stock recost <code>` rebuilds an average cost from history.")
            raise typer.Exit(1)

    run(action)


@cli.command("history")
def history(
    reference: Annotated[str, typer.Argument(help="Invoice number, or first chars of a sale id")],
    limit: Annotated[int, typer.Option("--limit", help="How many entries")] = 40,
) -> None:
    """Everything that has ever happened to one bill or sale, and who did it."""

    async def action(ctx: AdminContext) -> None:
        try:
            entity_id = (await resolve.purchase_by_invoice(ctx.session, ctx.org_id, reference)).id
            title = f"Purchase {reference}"
        except AdminError:
            header = await resolve.sale_by_reference(ctx.session, ctx.org_id, reference)
            entity_id = header.id
            title = f"Sale {str(header.id)[:8]}"

        entries = list(
            (
                await ctx.session.execute(
                    select(AuditLog)
                    .where(AuditLog.org_id == ctx.org_id, AuditLog.entity_id == entity_id)
                    .order_by(AuditLog.created_at)
                    .limit(limit)
                )
            ).scalars()
        )
        if not entries:
            console.warn("no audit entries -- this predates auditing, or the id is wrong.")
            return

        console.head(title)
        rows = []
        for entry in entries:
            who = (
                await ctx.session.execute(
                    select(User.full_name).where(User.id == entry.actor_user_id)
                )
            ).scalar_one_or_none()
            rows.append(
                [
                    entry.created_at.strftime("%Y-%m-%d %H:%M"),
                    entry.action,
                    entry.channel,
                    who or "—",
                ]
            )
        console.table(rows, headers=["when", "what", "how", "who"])

    run(action)


@cli.command("backup")
def backup() -> None:
    """Take one now. One is taken automatically before every change."""

    async def action(ctx: AdminContext) -> None:
        record = await BackupService(ctx.session).create_backup(ctx.org_id)
        console.ok(Path(record.file_path).name)

    run(action)


@cli.command("backups")
def backups() -> None:
    """List what can be restored."""

    async def action(ctx: AdminContext) -> None:
        records = BackupService(ctx.session).list_backups()
        if not records:
            console.warn("no backups yet")
            return
        console.table(
            [
                [
                    Path(r.file_path).name,
                    r.created_at.strftime("%Y-%m-%d %H:%M"),
                    f"{r.size_bytes / 1024:.0f} KB",
                ]
                for r in records
            ],
            headers=["name", "taken", "size"],
        )

    run(action)


@cli.command("restore")
def restore(
    name: Annotated[str, typer.Argument(help="Backup name, from `erp backups`")],
) -> None:
    """Replace the current books with a backup.

    This discards everything done since that backup was taken, including
    work that had nothing to do with whatever went wrong. Prefer undoing
    the specific thing."""

    async def action(ctx: AdminContext) -> None:
        console.warn(f"restoring {name} DISCARDS every change made since it was taken.")
        confirm(ctx, expected=name, prompt=f"Type the backup name to confirm ({name}): ")
        message = await BackupService(ctx.session).restore(backup_name=name, confirmation=name)
        console.ok(message)

    run(action)
