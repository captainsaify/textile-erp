"""Report generation -- docs/13_Reports.md, driven by the
`report_generation` task (docs/11_BackgroundWorkers.md §8).

`export` is asynchronous by design: a multi-month workbook can't be
built inside a WhatsApp webhook's response window. One `report_jobs`
row carries the status, so the follow-up message and any future API
polling read the same source rather than each tracking progress
separately.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.exceptions import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.models import (
    Product,
    PurchaseHeader,
    PurchaseLine,
    ReportJob,
    SalesHeader,
    SalesLine,
    Supplier,
    User,
)
from backend.reports.excel.purchase_sheet_template import PurchaseSheetRow, build_purchase_sheet
from backend.reports.excel.styling import (
    MONEY_FORMAT,
    QTY_FORMAT,
    autosize,
    write_header,
    write_row,
)

logger = get_logger(__name__)
ZERO = decimal.Decimal("0")

REPORT_TYPES = ("purchases", "sales", "stock")
LINK_EXPIRY_DAYS = 7


@dataclasses.dataclass(frozen=True)
class GeneratedReport:
    job_id: uuid.UUID
    status: str
    message: str
    notify_number: str | None
    #: Set when the report exists on disk, so the notifier can deliver
    #: the file itself. Telling someone a filename they have no way to
    #: reach is not a report they received.
    file_path: Path | None = None


class ReportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        actor: User,
        *,
        report_type: str,
        start: datetime.date,
        end: datetime.date,
    ) -> ReportJob:
        if report_type not in REPORT_TYPES:
            raise ValidationError(
                f"'{report_type}' isn't a report I can export. Try: {', '.join(REPORT_TYPES)}."
            )
        job = ReportJob(
            org_id=actor.org_id,
            report_type=report_type,
            output_format="excel",
            period_start=start,
            period_end=end,
            status="queued",
            expires_at=datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(days=LINK_EXPIRY_DAYS),
            created_by=actor.id,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def generate(self, job_id: uuid.UUID) -> GeneratedReport:
        # Explicit commits rather than `session.begin()` blocks: building
        # a workbook issues reads, which autobegin a transaction, and a
        # later begin() on the same session would raise.
        job = await self._session.get(ReportJob, job_id)
        if job is None:
            raise NotFoundError("report job", str(job_id))

        job.status = "generating"
        await self._session.commit()
        try:
            path, rows = await self._build(job)
        except Exception as exc:  # noqa: BLE001 -- surfaced on the job row
            logger.error("report_generation_failed", job_id=str(job_id), error=str(exc))
            await self._session.rollback()
            job = await self._session.get(ReportJob, job_id)
            assert job is not None
            job.status = "failed"
            job.error = str(exc)[:500]
            await self._session.commit()
            return GeneratedReport(
                job_id=job_id,
                status="failed",
                message=f"❌ That export failed: {exc}",
                notify_number=await self._notify_number(job),
            )

        job.status = "ready"
        job.file_path = str(path)
        job.file_size_bytes = path.stat().st_size
        job.row_count = rows
        await self._session.commit()
        return GeneratedReport(
            job_id=job_id,
            status="ready",
            message=f"📄 Your {job.report_type} export — {rows} row(s).",
            notify_number=await self._notify_number(job),
            file_path=path,
        )

    async def _notify_number(self, job: ReportJob) -> str | None:
        user = await self._session.get(User, job.created_by)
        return user.whatsapp_number if user else None

    def _output_path(self, job: ReportJob) -> Path:
        directory = Path(get_settings().attachments_dir).parent / "reports" / str(job.org_id)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
        return directory / f"{job.report_type}-{stamp}-{str(job.id)[:8]}.xlsx"

    async def _build(self, job: ReportJob) -> tuple[Path, int]:
        builders = {
            "purchases": self._build_purchases,
            "sales": self._build_sales,
            "stock": self._build_stock,
        }
        return await builders[job.report_type](job)

    async def _build_purchases(self, job: ReportJob) -> tuple[Path, int]:
        """The legacy-format export (docs/13_Reports.md §5). `pieces` and
        `weight_per_unit` come from the purchase line as captured from
        the original sheet, so a re-export reads like the sheet that was
        photographed."""
        stmt = (
            select(PurchaseLine, PurchaseHeader, Product, Supplier)
            .join(PurchaseHeader, PurchaseHeader.id == PurchaseLine.purchase_header_id)
            .join(Product, Product.id == PurchaseLine.product_id)
            .join(Supplier, Supplier.id == PurchaseHeader.supplier_id)
            .where(
                PurchaseHeader.org_id == job.org_id,
                PurchaseHeader.deleted_at.is_(None),
                PurchaseHeader.status == "confirmed",
                PurchaseHeader.invoice_date >= job.period_start,
                PurchaseHeader.invoice_date <= job.period_end,
            )
            .order_by(PurchaseHeader.invoice_date, PurchaseHeader.invoice_no, PurchaseLine.line_no)
        )
        rows = [
            PurchaseSheetRow(
                serial=index,
                pieces=line.weight_kg and (line.qty / line.weight_kg) or None,
                description=line.description or product.description,
                code=product.code,
                label=supplier.name,
                weight_per_unit=line.weight_kg,
                total_weight=line.total_weight_kg or line.qty,
            )
            for index, (line, _header, product, supplier) in enumerate(
                (await self._session.execute(stmt)).all(), start=1
            )
        ]
        workbook = build_purchase_sheet(rows, title="Purchases")
        path = self._output_path(job)
        workbook.save(path)
        return path, len(rows)

    async def _build_sales(self, job: ReportJob) -> tuple[Path, int]:
        from openpyxl import Workbook

        from backend.models import Customer

        stmt = (
            select(SalesLine, SalesHeader, Product, Customer)
            .join(SalesHeader, SalesHeader.id == SalesLine.sales_header_id)
            .join(Product, Product.id == SalesLine.product_id)
            .join(Customer, Customer.id == SalesHeader.customer_id)
            .where(
                SalesHeader.org_id == job.org_id,
                SalesHeader.deleted_at.is_(None),
                SalesHeader.status.in_(["confirmed", "partially_returned"]),
                SalesHeader.sale_date >= job.period_start,
                SalesHeader.sale_date <= job.period_end,
            )
            .order_by(SalesHeader.sale_date, SalesHeader.created_at, SalesLine.line_no)
        )
        headers = ["DATE", "CUSTOMER", "CODE", "DESCRIPTION", "QTY", "RATE", "AMOUNT", "COST"]
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.title = "Sales"
        write_header(sheet, headers)

        formats = {5: QTY_FORMAT, 6: MONEY_FORMAT, 7: MONEY_FORMAT, 8: MONEY_FORMAT}
        total_amount = ZERO
        total_cost = ZERO
        records = (await self._session.execute(stmt)).all()
        for offset, (line, header, product, customer) in enumerate(records, start=2):
            cost = (line.qty * line.avg_cost_at_sale_time).quantize(decimal.Decimal("0.01"))
            total_amount += line.line_total
            total_cost += cost
            write_row(
                sheet,
                offset,
                [
                    header.sale_date.strftime("%d-%m-%Y"),
                    customer.name,
                    product.code,
                    product.description,
                    float(line.qty),
                    float(line.rate),
                    float(line.line_total),
                    float(cost),
                ],
                formats=formats,
            )
        write_row(
            sheet,
            len(records) + 2,
            ["TOTAL", "", "", "", "", "", float(total_amount), float(total_cost)],
            formats=formats,
            bold=True,
        )
        autosize(sheet, headers)
        sheet.freeze_panes = "A2"
        path = self._output_path(job)
        workbook.save(path)
        return path, len(records)

    async def _build_stock(self, job: ReportJob) -> tuple[Path, int]:
        from openpyxl import Workbook

        from backend.models import Inventory

        stmt = (
            select(Product, Inventory)
            .join(Inventory, Inventory.product_id == Product.id)
            .where(
                Product.org_id == job.org_id,
                Product.deleted_at.is_(None),
                Product.is_active.is_(True),
            )
            .order_by(Product.code)
        )
        headers = ["CODE", "DESCRIPTION", "ON HAND", "AVG COST", "STOCK VALUE", "REORDER LEVEL"]
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.title = "Stock"
        write_header(sheet, headers)

        formats = {3: QTY_FORMAT, 4: MONEY_FORMAT, 5: MONEY_FORMAT, 6: QTY_FORMAT}
        total_value = ZERO
        records = (await self._session.execute(stmt)).all()
        for offset, (product, inventory) in enumerate(records, start=2):
            value = (inventory.qty_on_hand * inventory.weighted_avg_cost).quantize(
                decimal.Decimal("0.01")
            )
            total_value += value
            write_row(
                sheet,
                offset,
                [
                    product.code,
                    product.description,
                    float(inventory.qty_on_hand),
                    float(inventory.weighted_avg_cost),
                    float(value),
                    float(product.reorder_level) if product.reorder_level is not None else "",
                ],
                formats=formats,
            )
        write_row(
            sheet,
            len(records) + 2,
            ["TOTAL", "", "", "", float(total_value), ""],
            formats=formats,
            bold=True,
        )
        autosize(sheet, headers)
        sheet.freeze_panes = "A2"
        path = self._output_path(job)
        workbook.save(path)
        return path, len(records)
