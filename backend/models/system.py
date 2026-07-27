"""audit_logs, attachments, whatsapp_sessions, settings
-- docs/02_Database.md §3.18-3.21.

audit_logs is append-only and partitioned by month (§9): composite
(id, created_at) PK, same rationale as inventory_movements.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class AuditLog(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp','api','dashboard','system')", name="channel_valid"),
        Index("idx_audit_logs_org_entity", "org_id", "entity_type", "entity_id"),
        Index("idx_audit_logs_org_created", "org_id", "created_at"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    whatsapp_message_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        primary_key=True, server_default=text("now()")
    )


class Attachment(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded','processing','processed','failed')", name="status_valid"
        ),
    )

    file_path: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # exact-duplicate photo detection -- docs/04_Purchases.md
    sha256_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="uploaded")
    ocr_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    whatsapp_media_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class WhatsappSession(UUIDPkMixin, OrgScopedMixin, Base):
    """Durable mirror of the Redis session cache -- docs/02_Database.md §3.20."""

    __tablename__ = "whatsapp_sessions"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))


class Setting(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSONB, nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class ReconciliationRun(UUIDPkMixin, OrgScopedMixin, Base):
    """Auditable proof a nightly check actually ran --
    docs/11_BackgroundWorkers.md §6.3. A successful run is recorded, not
    left as silence implying success: "no alert" and "the job never ran"
    must be distinguishable after the fact."""

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint("kind IN ('inventory','ledger')", name="kind_valid"),
        CheckConstraint("status IN ('ok','mismatch','failed')", name="status_valid"),
        Index("idx_reconciliation_runs_org_kind", "org_id", "kind", "started_at"),
    )

    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    checked_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: full per-item detail of anything that disagreed, for engineering
    #: follow-up -- never used to auto-correct (§6.4)
    details: Mapped[list[Any] | None] = mapped_column(JSONB)
    #: cleared when an owner explicitly acknowledges; a mismatch stays
    #: visible on the dashboard until then
    acknowledged_at: Mapped[datetime.datetime | None]
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    started_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    finished_at: Mapped[datetime.datetime | None]


class ReportJob(UUIDPkMixin, OrgScopedMixin, Base):
    """`export` is asynchronous (docs/13_Reports.md §1), so the WhatsApp
    follow-up and the API's polling endpoint read one shared status row
    rather than each tracking progress their own way."""

    __tablename__ = "report_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','generating','ready','failed')", name="status_valid"),
        Index("idx_report_jobs_org_created", "org_id", "created_at"),
    )

    report_type: Mapped[str] = mapped_column(String, nullable=False)
    output_format: Mapped[str] = mapped_column(String, nullable=False, server_default="excel")
    period_start: Mapped[datetime.date | None]
    period_end: Mapped[datetime.date | None]
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="queued")
    file_path: Mapped[str | None] = mapped_column(String)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String)
    expires_at: Mapped[datetime.datetime | None]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
