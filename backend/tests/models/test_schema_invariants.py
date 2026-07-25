"""Metadata-level invariants from docs/02_Database.md §1 -- enforced
mechanically so precision/timezone/org-scoping rules can't drift as
tables are added. No database required."""

from __future__ import annotations

import sqlalchemy as sa

from backend.models import Base

# §3.17: journal_lines is a pure child of journal; §3.1: organizations is
# the tenancy root itself.
TABLES_WITHOUT_ORG_ID = {"organizations", "journal_lines"}

# §9: append-only ledgers -- corrections are new offsetting rows, so the
# columns that imply in-place mutation must not exist.
APPEND_ONLY_TABLES = {"inventory_movements", "audit_logs", "cash_ledger", "bank_ledger"}

# §1: money NUMERIC(14,2), qty/weight NUMERIC(12,3), rate NUMERIC(12,4),
# ocr confidence NUMERIC(4,3), percent NUMERIC(5,2).
ALLOWED_NUMERIC_PRECISIONS = {(14, 2), (12, 3), (12, 4), (4, 3), (5, 2)}


def test_no_float_columns() -> None:
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, sa.Float | sa.Double)
    ]
    assert not offenders, f"float columns are forbidden (docs/02_Database.md): {offenders}"


def test_all_numeric_columns_use_documented_precisions() -> None:
    offenders = [
        f"{table.name}.{col.name} = NUMERIC({col.type.precision},{col.type.scale})"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, sa.Numeric)
        and not isinstance(col.type, sa.Float)
        and (col.type.precision, col.type.scale) not in ALLOWED_NUMERIC_PRECISIONS
    ]
    assert not offenders, f"undocumented NUMERIC precision: {offenders}"


def test_all_timestamps_are_timezone_aware() -> None:
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, sa.DateTime) and not col.type.timezone
    ]
    assert not offenders, f"naive TIMESTAMP forbidden, use TIMESTAMPTZ: {offenders}"


def test_every_table_has_uuid_id_primary_key() -> None:
    for table in Base.metadata.tables.values():
        pk_names = {col.name for col in table.primary_key.columns}
        assert "id" in pk_names, f"{table.name} primary key must include id"
        assert isinstance(table.columns["id"].type, sa.Uuid), f"{table.name}.id must be UUID"


def test_every_business_table_is_org_scoped() -> None:
    for table in Base.metadata.tables.values():
        if table.name in TABLES_WITHOUT_ORG_ID:
            assert "org_id" not in table.columns
            continue
        assert "org_id" in table.columns, f"{table.name} must have org_id"
        col = table.columns["org_id"]
        assert not col.nullable, f"{table.name}.org_id must be NOT NULL"
        fk_targets = {fk.column.table.name for fk in col.foreign_keys}
        assert fk_targets == {"organizations"}, f"{table.name}.org_id must FK organizations"


def test_append_only_tables_have_no_mutation_columns() -> None:
    for name in APPEND_ONLY_TABLES:
        table = Base.metadata.tables[name]
        assert "updated_at" not in table.columns, f"{name} is append-only"
        assert "deleted_at" not in table.columns, f"{name} is append-only"


def test_soft_delete_tables_have_creation_audit_columns() -> None:
    for table in Base.metadata.tables.values():
        if "deleted_at" not in table.columns:
            continue
        assert "created_at" in table.columns, f"{table.name} needs created_at"
        # documented exceptions in the DDL: users (is the identity table),
        # brands (§3.5 carries no created_by)
        if table.name not in {"users", "brands"}:
            assert "created_by" in table.columns, f"{table.name} needs created_by"
