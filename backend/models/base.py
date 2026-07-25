"""Declarative base + shared column-type constants.

Numeric precisions match docs/02_Database.md §1 exactly:
money NUMERIC(14,2), qty/weight NUMERIC(12,3), rate NUMERIC(12,4),
confidence NUMERIC(4,3). NUMERIC (never FLOAT) is non-negotiable for a
ledger system -- see docs/17_CodingStandards.md §1. Every model uses
these constants rather than declaring `Numeric(...)` inline, so the
precision is defined once and can't drift table to table.
"""

from __future__ import annotations

import datetime

from sqlalchemy import MetaData, Numeric
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # All timestamps are TIMESTAMPTZ, stored UTC -- docs/02_Database.md §8.
    # Without this, Mapped[datetime.datetime] silently maps to a naive
    # TIMESTAMP WITHOUT TIME ZONE.
    type_annotation_map = {  # noqa: RUF012 -- SQLAlchemy declarative API
        datetime.datetime: TIMESTAMP(timezone=True),
    }


MONEY = Numeric(14, 2)
QTY = Numeric(12, 3)
RATE = Numeric(12, 4)
CONFIDENCE = Numeric(4, 3)
