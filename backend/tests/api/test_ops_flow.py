"""Reconciliation, export and backup -- docs/11_BackgroundWorkers.md
§5/§6/§8, docs/03_Inventory.md §6, docs/13_Reports.md §5.

The reconciliation tests are the ones that matter most: they check
CLAUDE.md's standing acceptance criterion (qty_on_hand equals the signed
sum of movements) against real rows, and they check that a detected
mismatch is *reported and not repaired* — a job that quietly fixed the
number would destroy the only evidence that something upstream is
broken.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.ops_commands import handle_backup, handle_export, handle_restore
from backend.core.config import get_settings
from backend.models import (
    Inventory,
    Product,
    PurchaseHeader,
    PurchaseLine,
    ReconciliationRun,
    ReportJob,
    Supplier,
    User,
)
from backend.reports.excel.purchase_sheet_template import COLUMNS, build_purchase_sheet
from backend.services.inventory_service import InventoryService
from backend.services.reconciliation_service import ReconciliationService
from backend.services.report_service import ReportService
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
    purge_business_rows,
)

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)
WAREHOUSE = uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(owner_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory, message_id="m1")


async def _product_with_movement(
    session: AsyncSession, actor: User, *, qty: str = "50", landed: str = "160"
) -> tuple[uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:6]
    product = Product(
        org_id=ORG,
        product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
        code=f"REC{suffix.upper()}",
        description="Reconciliation Test Fabric",
        unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
        created_by=actor.id,
    )
    session.add(product)
    await session.flush()
    supplier = Supplier(org_id=ORG, name=f"Supp {suffix}", created_by=actor.id)
    session.add(supplier)
    await session.flush()
    header = PurchaseHeader(
        org_id=ORG,
        supplier_id=supplier.id,
        warehouse_id=WAREHOUSE,
        invoice_no=f"INV-{suffix}",
        invoice_date=datetime.date.today(),
        grand_total=(D(qty) * D(landed)),
        status="confirmed",
        created_by=actor.id,
    )
    session.add(header)
    await session.flush()
    line = PurchaseLine(
        org_id=ORG,
        purchase_header_id=header.id,
        line_no=1,
        product_id=product.id,
        description="Reconciliation Test Fabric",
        qty=D(qty),
        weight_kg=D("10"),
        total_weight_kg=D(qty),
        rate=D(landed),
        line_total=(D(qty) * D(landed)),
        landed_cost_per_unit=D(landed),
    )
    session.add(line)
    await session.flush()
    await InventoryService(session).record_purchase_movement(
        ORG,
        product_id=product.id,
        warehouse_id=WAREHOUSE,
        qty=D(qty),
        landed_cost_per_unit=D(landed),
        source_id=line.id,
        created_by=actor.id,
    )
    return product.id, product.code


# --------------------------------------------------------------------
# inventory reconciliation
# --------------------------------------------------------------------


async def test_reconciliation_passes_when_the_cache_matches_the_replay(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        await _product_with_movement(session, ctx.user)

    async with session_factory() as session, session.begin():
        outcome = await ReconciliationService(session).run(ORG, "inventory")
    assert outcome.ok
    assert outcome.checked == 1


async def test_a_successful_run_is_recorded_not_left_as_silence(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/11_BackgroundWorkers.md §6.3 -- otherwise "nothing was wrong"
    and "the job never fired" look identical afterwards."""
    async with session_factory() as session, session.begin():
        await _product_with_movement(session, ctx.user)
    async with session_factory() as session, session.begin():
        await ReconciliationService(session).run(ORG, "inventory")

    async with session_factory() as session:
        run = (
            await session.execute(
                sa.select(ReconciliationRun).where(ReconciliationRun.kind == "inventory")
            )
        ).scalar_one()
    assert run.status == "ok"
    assert run.checked_count == 1
    assert run.finished_at is not None


async def test_reconciliation_detects_a_tampered_cache_and_does_not_fix_it(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The core promise: detect, never silently correct
    (docs/03_Inventory.md §6)."""
    async with session_factory() as session, session.begin():
        product_id, code = await _product_with_movement(session, ctx.user)

    # simulate the thing this job exists to catch: the cached balance
    # drifting away from the movements that justify it
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = 999 WHERE product_id = :pid"),
            {"pid": product_id},
        )

    async with session_factory() as session, session.begin():
        outcome = await ReconciliationService(session).run(ORG, "inventory")

    assert not outcome.ok
    assert outcome.discrepancies[0].subject == code
    assert outcome.discrepancies[0].cached == "999.000"
    assert outcome.discrepancies[0].replayed == "50.000"

    # and the wrong number is still there -- untouched
    async with session_factory() as session:
        qty = (
            await session.execute(
                sa.select(Inventory.qty_on_hand).where(Inventory.product_id == product_id)
            )
        ).scalar_one()
    assert qty == D("999.000")


async def test_mismatch_alert_names_both_numbers_and_says_it_did_nothing(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        product_id, code = await _product_with_movement(session, ctx.user)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = 12 WHERE product_id = :pid"),
            {"pid": product_id},
        )
    async with session_factory() as session, session.begin():
        outcome = await ReconciliationService(session).run(ORG, "inventory")

    text = outcome.alert_text()
    assert code in text
    assert "12.000" in text and "50.000" in text
    assert "Not auto-corrected" in text


async def test_mismatch_detail_is_persisted_for_follow_up(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        product_id, _ = await _product_with_movement(session, ctx.user)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = 1 WHERE product_id = :pid"),
            {"pid": product_id},
        )
    async with session_factory() as session, session.begin():
        await ReconciliationService(session).run(ORG, "inventory")

    async with session_factory() as session:
        run = (
            await session.execute(
                sa.select(ReconciliationRun).where(ReconciliationRun.status == "mismatch")
            )
        ).scalar_one()
    assert run.mismatch_count == 1
    assert run.details is not None
    assert run.details[0]["replayed"] == "50.000"


# --------------------------------------------------------------------
# ledger reconciliation
# --------------------------------------------------------------------


async def test_ledger_reconciliation_passes_on_clean_books(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.api.commands.money_commands import handle_expense

    await handle_expense("transport 500 cash", ctx)
    async with session_factory() as session, session.begin():
        outcome = await ReconciliationService(session).run(ORG, "ledger")
    assert outcome.ok


async def test_ledger_reconciliation_catches_a_broken_running_balance(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.api.commands.money_commands import handle_expense

    await handle_expense("transport 500 cash", ctx)
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("UPDATE cash_ledger SET resulting_balance = -99999"))

    async with session_factory() as session, session.begin():
        outcome = await ReconciliationService(session).run(ORG, "ledger")
    assert not outcome.ok
    assert "cash balance" in outcome.discrepancies[0].subject


# --------------------------------------------------------------------
# the purchases export
# --------------------------------------------------------------------


def test_purchase_sheet_uses_the_partners_column_order() -> None:
    """docs/13_Reports.md §5 -- the legacy layout is the point of this
    export; a refactor must not quietly reorder it."""
    assert [header for header, _ in COLUMNS] == [
        "S.NO",
        "QTY",
        "DESCRIPTION",
        "CODE",
        "LABEL",
        "KG",
        "T.KG",
    ]


def test_purchase_sheet_writes_headers_rows_and_a_totals_row(tmp_path: object) -> None:
    from backend.reports.excel.purchase_sheet_template import PurchaseSheetRow

    rows = [
        PurchaseSheetRow(
            serial=1,
            pieces=D("10"),
            description="Men Zipper Jacket",
            code="35A",
            label="Wagdia",
            weight_per_unit=D("80"),
            total_weight=D("800"),
        ),
        PurchaseSheetRow(
            serial=2,
            pieces=D("19"),
            description="Corduroy Pant",
            code="VVP-1",
            label="Wagdia",
            weight_per_unit=D("80"),
            total_weight=D("1520"),
        ),
    ]
    workbook = build_purchase_sheet(rows)
    sheet = workbook.active
    assert sheet is not None
    assert [cell.value for cell in sheet[1]] == [
        "S.NO",
        "QTY",
        "DESCRIPTION",
        "CODE",
        "LABEL",
        "KG",
        "T.KG",
    ]
    assert sheet.cell(row=2, column=4).value == "35A"
    # totals row: QTY and T.KG summed, KG (a per-unit rate) left blank
    total_row = len(rows) + 2
    assert sheet.cell(row=total_row, column=1).value == "TOTAL"
    assert sheet.cell(row=total_row, column=2).value == 29
    assert sheet.cell(row=total_row, column=7).value == 2320
    assert sheet.cell(row=total_row, column=6).value == ""
    assert sheet.cell(row=total_row, column=1).font.bold


async def test_export_enqueues_a_job_and_reports_the_reference(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    result = await handle_export("purchases month", ctx)
    assert "Building your purchases export" in result.reply

    async with session_factory() as session:
        job = (await session.execute(sa.select(ReportJob))).scalar_one()
    assert job.report_type == "purchases"
    assert job.status == "queued"
    assert str(job.id)[:8] in result.reply


async def test_export_rejects_an_unknown_report(ctx: RequestContext) -> None:
    result = await handle_export("everything month", ctx)
    assert "isn't a report I can export" in result.reply


async def test_generated_purchases_workbook_contains_the_data(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        _, code = await _product_with_movement(session, ctx.user)

    async with session_factory() as session, session.begin():
        job = await ReportService(session).enqueue(
            ctx.user,
            report_type="purchases",
            start=datetime.date.today() - datetime.timedelta(days=1),
            end=datetime.date.today() + datetime.timedelta(days=1),
        )
        job_id = job.id
    async with session_factory() as session:
        report = await ReportService(session).generate(job_id)

    assert report.status == "ready"
    async with session_factory() as session:
        stored = await session.get(ReportJob, job_id)
        assert stored is not None
        assert stored.row_count == 1
        workbook = load_workbook(stored.file_path)
    sheet = workbook.active
    assert sheet is not None
    assert sheet.cell(row=2, column=4).value == code


async def test_a_failed_report_records_the_error_on_the_job(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A failure has to be visible on the row both the follow-up message
    and any API poll read, not only in the logs."""
    async with session_factory() as session, session.begin():
        job = await ReportService(session).enqueue(
            ctx.user,
            report_type="purchases",
            start=datetime.date.today(),
            end=datetime.date.today(),
        )
        job_id = job.id
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE report_jobs SET report_type = 'nonsense' WHERE id = :id"),
            {"id": job_id},
        )

    async with session_factory() as session:
        report = await ReportService(session).generate(job_id)
    assert report.status == "failed"

    async with session_factory() as session:
        stored = await session.get(ReportJob, job_id)
        assert stored is not None
    assert stored.status == "failed"
    assert stored.error


# --------------------------------------------------------------------
# backup / restore guards
# --------------------------------------------------------------------


@pytest.fixture
def isolated_backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """BackupService derives its directory from settings.attachments_dir.
    Without redirecting it these tests read the developer's real backups
    and pass or fail depending on what happens to be on disk."""
    from backend.core.config import Settings
    from backend.services import backup_service

    real = get_settings()
    patched = Settings(**{**real.model_dump(), "attachments_dir": str(tmp_path / "attachments")})
    monkeypatch.setattr(backup_service, "get_settings", lambda: patched)
    return tmp_path / "backups"


async def test_backup_listing_when_none_exist(
    ctx: RequestContext, isolated_backup_dir: Path
) -> None:
    result = await handle_backup("", ctx)
    assert "No backups yet" in result.reply


async def test_restore_without_confirmation_explains_the_risk(ctx: RequestContext) -> None:
    """A restore that one mistyped word could trigger is a data-loss
    incident waiting to happen."""
    result = await handle_restore("backup-20260101T000000Z-abc.dump", ctx)
    assert "replaces ALL current data" in result.reply
    assert "confirm" in result.reply


async def test_restore_refuses_a_mismatched_confirmation(ctx: RequestContext) -> None:
    result = await handle_restore("backup-a.dump confirm backup-b.dump", ctx)
    assert "To confirm" in result.reply or "No backup named" in result.reply


async def test_ops_command_permissions() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY
    from backend.models.enums import UserRole

    assert COMMAND_REGISTRY["export"].min_role == UserRole.STAFF
    assert COMMAND_REGISTRY["backup"].min_role == UserRole.OWNER
    assert COMMAND_REGISTRY["restore"].min_role == UserRole.OWNER


def test_beat_schedule_covers_every_scheduled_task() -> None:
    """A task that exists but is never scheduled is dead code; one that
    is scheduled but doesn't exist fails only at 2am."""
    from backend.workers import tasks as task_module
    from backend.workers.schedule import CELERYBEAT_SCHEDULE

    for entry in CELERYBEAT_SCHEDULE.values():
        name = entry["task"]
        assert hasattr(task_module, name), f"{name} is scheduled but not defined"


def test_the_schedule_is_actually_attached_to_the_celery_app() -> None:
    """`celery beat` reads beat_schedule off the configured app. A
    schedule that is defined but never assigned gives a Beat process
    that starts cleanly and fires nothing — silently, forever."""
    from backend.workers.app import celery_app
    from backend.workers.schedule import CELERYBEAT_SCHEDULE

    assert celery_app.conf.beat_schedule == CELERYBEAT_SCHEDULE
    assert celery_app.conf.beat_schedule, "beat has no entries"


def test_every_scheduled_task_is_registered_with_the_app() -> None:
    """Beat dispatches by task *name*; a name Beat knows but the app
    doesn't produces an unroutable message at 2am, not an import error
    at deploy time."""
    from backend.workers import tasks  # noqa: F401 -- registers the tasks
    from backend.workers.app import celery_app
    from backend.workers.schedule import CELERYBEAT_SCHEDULE

    for entry in CELERYBEAT_SCHEDULE.values():
        assert entry["task"] in celery_app.tasks, f"{entry['task']} not registered"


# --------------------------------------------------------------------
# delivering the report, not just announcing it
# --------------------------------------------------------------------


class RecordingClient:
    """Stands in for the Cloud API client; records what was sent."""

    def __init__(self, *, upload_succeeds: bool = True) -> None:
        self.upload_succeeds = upload_succeeds
        self.texts: list[tuple[str, str]] = []
        self.documents: list[tuple[str, Path, str, str]] = []

    async def send_text(self, to_number: str, body: str) -> bool:
        self.texts.append((to_number, body))
        return True

    async def send_document(
        self, to_number: str, path: Path, *, filename: str, caption: str
    ) -> bool:
        self.documents.append((to_number, path, filename, caption))
        return self.upload_succeeds


async def _finished_report(tmp_path: Path, client: object) -> None:
    from backend.services import whatsapp_client as client_module
    from backend.services.report_service import GeneratedReport
    from backend.workers.tasks import _deliver_report

    workbook = tmp_path / "purchases-20260728-abcdef.xlsx"
    workbook.write_bytes(b"xlsx")
    record = GeneratedReport(
        job_id=uuid.uuid4(),
        status="ready",
        message="📄 Your purchases export — 26 row(s).",
        notify_number="+919000000000",
        file_path=workbook,
    )
    original = client_module.get_whatsapp_client
    client_module.get_whatsapp_client = lambda: client  # type: ignore[assignment,return-value]
    try:
        await _deliver_report(record)
    finally:
        client_module.get_whatsapp_client = original


async def test_a_ready_report_arrives_as_a_file(tmp_path: Path) -> None:
    """The old message named a file inside a container and said it would
    expire -- neither of which the recipient could act on. Nothing was
    ever uploaded."""
    client = RecordingClient()
    await _finished_report(tmp_path, client)

    assert len(client.documents) == 1
    _, path, filename, caption = client.documents[0]
    assert filename == "purchases-20260728-abcdef.xlsx"
    assert path.read_bytes() == b"xlsx"
    assert "26 row(s)" in caption
    assert client.texts == [], "the caption carries the message; a separate text repeats it"


async def test_a_failed_upload_says_so_instead_of_claiming_delivery(tmp_path: Path) -> None:
    client = RecordingClient(upload_succeeds=False)
    await _finished_report(tmp_path, client)

    assert len(client.documents) == 1
    assert len(client.texts) == 1
    assert "couldn't attach the file" in client.texts[0][1]


async def test_a_transport_without_files_falls_back_to_text(tmp_path: Path) -> None:
    """The whatsapp-web.js bridge has no send_document; the flow still
    completes rather than raising (docs/19 §3)."""

    class TextOnlyClient:
        def __init__(self) -> None:
            self.texts: list[tuple[str, str]] = []

        async def send_text(self, to_number: str, body: str) -> bool:
            self.texts.append((to_number, body))
            return True

    client = TextOnlyClient()
    await _finished_report(tmp_path, client)

    assert len(client.texts) == 1
    assert "26 row(s)" in client.texts[0][1]
