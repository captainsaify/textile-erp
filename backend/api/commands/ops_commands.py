"""`export`, `backup`, `restore` -- docs/08_WhatsApp.md, backed by the
Celery tasks in docs/11_BackgroundWorkers.md §5/§8.

`export` returns immediately with a job reference and the worker sends
the result when it's built: a multi-month workbook cannot be produced
inside a webhook's response window.
"""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_date
from backend.api.period import parse_period
from backend.core.exceptions import DomainError, ValidationError
from backend.repositories.accounting_repository import business_today
from backend.services.backup_service import BackupService
from backend.services.report_service import REPORT_TYPES, ReportService

EXPORT_USAGE = (
    "Usage: export <purchases|sales|stock> [today|week|month|year|<DD-MM-YYYY> to <DD-MM-YYYY>]"
)
RESTORE_USAGE = "Usage: restore <backup-name> confirm <backup-name>"


async def handle_export(args: str, ctx: RequestContext) -> CommandResult:
    parts = args.split(maxsplit=1)
    if not parts:
        # Reached only by a direct caller: over WhatsApp the wizard asks
        # for the report and the period first (docs/20 §7).
        return CommandResult(reply=EXPORT_USAGE)
    report_type = parts[0].strip().lower()
    if report_type not in REPORT_TYPES:
        return CommandResult(
            reply=f"'{report_type}' isn't a report I can export. "
            f"Try: {', '.join(REPORT_TYPES)}.\n{EXPORT_USAGE}"
        )
    period_args = parts[1] if len(parts) > 1 else "month"

    try:
        async with ctx.session_factory() as session, session.begin():
            today = await business_today(session, ctx.user.org_id)
            period = parse_period(period_args, today)
            job = await ReportService(session).enqueue(
                ctx.user,
                report_type=report_type,
                start=period.start,
                end=period.end,
            )
            job_id = job.id
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    _dispatch_report(str(job_id))
    return CommandResult(
        reply=(
            f"⏳ Building your {report_type} export for {period.label} "
            f"({fmt_date(period.start)} – {fmt_date(period.end)}).\n"
            f"Reference {str(job_id)[:8]} — I'll message you when it's ready."
        )
    )


def _dispatch_report(job_id: str) -> None:
    """Queue the work. If the broker is unreachable the command still
    succeeded in recording the job, so this is logged rather than
    raised -- the row stays `queued` and a retry can pick it up."""
    from backend.core.logging import get_logger

    try:
        from backend.workers.tasks import report_generation

        report_generation.apply_async(args=[job_id], queue="reports")
    except Exception as exc:  # noqa: BLE001 -- broker down must not lose the job
        get_logger(__name__).error("report_dispatch_failed", job_id=job_id, error=str(exc))


async def handle_backup(args: str, ctx: RequestContext) -> CommandResult:
    """`backup` lists what exists; `backup now` takes one immediately.
    The nightly job is the normal path (docs/11_BackgroundWorkers.md §5)
    -- this is for before something risky."""
    if args.strip().lower() in {"now", "run"}:
        try:
            async with ctx.session_factory() as session:
                record = await BackupService(session).create_backup(ctx.user.org_id)
        except Exception as exc:  # noqa: BLE001 -- surfaced, not swallowed
            return CommandResult(reply=f"❌ Backup failed: {exc}")
        name = record.file_path.rsplit("/", 1)[-1]
        return CommandResult(
            reply=(
                f"✅ Backup taken — {name}\n"
                f"{record.size_bytes // 1024} KB, checksum {record.checksum[:12]}…\n"
                + (
                    "Scanned invoices included."
                    if record.attachments_included
                    else "No scanned invoices to include yet."
                )
            )
        )

    async with ctx.session_factory() as session:
        records = BackupService(session).list_backups()
    if not records:
        return CommandResult(
            reply="No backups yet. The nightly job runs at 02:00, or send 'backup now'."
        )
    lines = [f"💾 {len(records)} backup(s):"]
    for record in records[:10]:
        name = record.file_path.rsplit("/", 1)[-1]
        lines.append(
            f"• {name} — {record.size_bytes // 1024} KB, "
            f"{record.created_at.strftime('%d-%m-%Y %H:%M')} UTC"
        )
    return CommandResult(reply="\n".join(lines))


async def handle_restore(args: str, ctx: RequestContext) -> CommandResult:
    """Overwrites everything, so the backup's name has to be typed twice
    -- once to choose it and once to mean it."""
    tokens = args.split()
    if len(tokens) != 3 or tokens[1].lower() != "confirm":
        name = tokens[0] if tokens else "<backup-name>"
        return CommandResult(
            reply=(
                "⚠️ Restoring replaces ALL current data with the backup's contents. "
                "Anything recorded since that backup will be lost.\n"
                f"To go ahead, send: restore {name} confirm {name}"
            )
        )
    try:
        async with ctx.session_factory() as session:
            path = await BackupService(session).restore(
                backup_name=tokens[0], confirmation=tokens[2]
            )
    except ValidationError as exc:
        return CommandResult(reply=exc.message)
    except Exception as exc:  # noqa: BLE001
        return CommandResult(reply=f"❌ Restore failed: {exc}")
    return CommandResult(
        reply=f"✅ Restored from {path.rsplit('/', 1)[-1]}. Check your balances before carrying on."
    )
