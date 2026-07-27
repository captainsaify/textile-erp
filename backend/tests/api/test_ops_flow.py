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

import pytest
import sqlalchemy as sa
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.ops_commands import handle_backup, handle_export, handle_restore
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


async def test_backup_listing_when_none_exist(ctx: RequestContext) -> None:
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
