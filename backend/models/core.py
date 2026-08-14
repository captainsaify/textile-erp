"""organizations, users, partners -- docs/02_Database.md §3.1-3.3."""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base
from backend.models.enums import UserRole, user_role_enum
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class Organization(UUIDPkMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String, nullable=False)
    base_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="INR")
    timezone: Mapped[str] = mapped_column(String, nullable=False, server_default="Asia/Kolkata")
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))

    users: Mapped[list[User]] = relationship(back_populates="organization")
    partners: Mapped[list[Partner]] = relationship(back_populates="organization")


class User(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "whatsapp_number IS NOT NULL OR email IS NOT NULL",
            name="login_method",
        ),
        # Soft-delete-aware uniqueness (docs/02_Database.md §4): a deleted
        # user's number/email can be re-registered.
        Index(
            "users_whatsapp_number_active_uq",
            "whatsapp_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "users_email_active_uq",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    full_name: Mapped[str] = mapped_column(String, nullable=False)
    whatsapp_number: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(CITEXT)
    password_hash: Mapped[str | None] = mapped_column(String)
    #: Master Control, which is a different proposition from the
    #: dashboard: one shows charts, the other can merge two customers
    #: and purge a bill (plan.md §4). NULL for everyone until someone
    #: deliberately runs `set-control-password`, so the danger surface
    #: does not exist by default.
    control_password_hash: Mapped[str | None] = mapped_column(String)
    role: Mapped[UserRole] = mapped_column(
        user_role_enum, nullable=False, server_default=UserRole.STAFF.value
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    deleted_at: Mapped[datetime.datetime | None]

    organization: Mapped[Organization] = relationship(back_populates="users")


class Partner(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "partners"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    profit_share_percent: Mapped[decimal.Decimal] = mapped_column(
        Numeric(5, 2),
        CheckConstraint(
            "profit_share_percent >= 0 AND profit_share_percent <= 100",
            name="profit_share_percent_range",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]

    organization: Mapped[Organization] = relationship(back_populates="partners")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
