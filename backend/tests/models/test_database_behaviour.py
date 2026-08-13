"""Behavioural checks against the real migrated schema: server-side
defaults, the updated_at trigger (docs/02_Database.md §5), soft-delete-
aware partial unique indexes (§4), seed rows (§6), and the monthly
partitions on the append-only tables (§9)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.models import Brand, Organization, User

SEEDED_ORG_ID = uuid.UUID("00000000-0000-4000-a000-000000000001")


@pytest.fixture
async def engine(migrated_test_db: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_test_db)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def test_seed_rows_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        org = (
            await conn.execute(
                sa.text("SELECT name, base_currency, timezone FROM organizations WHERE id = :id"),
                {"id": SEEDED_ORG_ID},
            )
        ).one()
        assert org.base_currency == "INR"
        assert org.timezone == "Asia/Kolkata"
        unit_codes = set((await conn.execute(sa.text("SELECT code FROM units"))).scalars().all())
        assert {"KG", "PCS", "MTR", "ROLL", "BOX"} <= unit_codes
        textile = (
            await conn.execute(
                sa.text("SELECT attribute_schema FROM product_types WHERE code = 'textile'")
            )
        ).scalar_one()
        assert textile["properties"]["gsm"]["type"] == "number"
        default_warehouses = (
            (await conn.execute(sa.text("SELECT name FROM warehouses WHERE is_default")))
            .scalars()
            .all()
        )
        assert default_warehouses == ["Main"]


async def test_server_side_defaults_and_tz(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    number = f"+9199{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        user = User(org_id=SEEDED_ORG_ID, full_name="Defaults Probe", whatsapp_number=number)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        try:
            assert isinstance(user.id, uuid.UUID)
            assert user.role.value == "staff"
            assert user.is_active is True
            assert user.created_at.tzinfo is not None, "created_at must be TIMESTAMPTZ"
        finally:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await session.commit()


async def test_updated_at_bumped_by_trigger_not_application(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    number = f"+9198{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        user = User(org_id=SEEDED_ORG_ID, full_name="Trigger Probe", whatsapp_number=number)
        session.add(user)
        await session.commit()
        user_id, first_updated_at = user.id, user.updated_at
        try:
            # separate transaction so now() differs from the insert's now()
            await session.execute(
                sa.text("UPDATE users SET full_name = 'Renamed' WHERE id = :id"),
                {"id": user_id},
            )
            await session.commit()
            second_updated_at = (
                await session.execute(
                    sa.text("SELECT updated_at FROM users WHERE id = :id"), {"id": user_id}
                )
            ).scalar_one()
            assert second_updated_at > first_updated_at
        finally:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await session.commit()


async def test_soft_delete_aware_unique_allows_reuse_after_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    name = f"probe-brand-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        first = Brand(org_id=SEEDED_ORG_ID, name=name)
        session.add(first)
        await session.commit()
        first_id = first.id  # rollback below expires ORM state; capture now
        try:
            session.add(Brand(org_id=SEEDED_ORG_ID, name=name))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            await session.execute(
                sa.text("UPDATE brands SET deleted_at = now() WHERE id = :id"),
                {"id": first_id},
            )
            await session.commit()

            second = Brand(org_id=SEEDED_ORG_ID, name=name)
            session.add(second)
            await session.commit()
        finally:
            await session.rollback()
            await session.execute(sa.text("DELETE FROM brands WHERE name = :n"), {"n": name})
            await session.commit()


async def test_monthly_partitions_exist_for_append_only_tables(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for parent in ("inventory_movements", "audit_logs", "cash_ledger", "bank_ledger"):
            children = (
                (
                    await conn.execute(
                        sa.text(
                            "SELECT c.relname FROM pg_inherits i "
                            "JOIN pg_class c ON c.oid = i.inhrelid "
                            "JOIN pg_class p ON p.oid = i.inhparent "
                            "WHERE p.relname = :parent"
                        ),
                        {"parent": parent},
                    )
                )
                .scalars()
                .all()
            )
            monthly = [c for c in children if c != f"{parent}_default"]
            assert f"{parent}_default" in children, f"{parent} needs a DEFAULT partition"
            assert len(monthly) >= 13, f"{parent} should have >= 13 monthly partitions"


async def test_organization_model_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        org = await session.get(Organization, SEEDED_ORG_ID)
        assert org is not None
        assert org.base_currency == "INR"


async def test_both_line_tables_carry_weight_the_same_way(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Purchases and sales must describe a quantity identically.

    The sheets have three quantity columns -- bales, kg per bale, and
    the two multiplied -- and `purchase_lines` carried them from the
    start while `sales_lines` did not. That asymmetry was invisible
    while both sides were typed into WhatsApp a field at a time, and
    becomes two screens that look like different products the moment
    there is a form. Same names, same precision, or the next query that
    joins them is subtly wrong on one side.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT table_name, column_name, numeric_precision, numeric_scale, "
                    "       is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name IN ('purchase_lines','sales_lines') "
                    "  AND column_name IN ('weight_kg','total_weight_kg') "
                    "ORDER BY column_name, table_name"
                )
            )
        ).all()

    shapes: dict[str, set[tuple[int, int, str]]] = {}
    for _table, column, precision, scale, nullable in rows:
        shapes.setdefault(column, set()).add((precision, scale, nullable))

    assert set(shapes) == {"weight_kg", "total_weight_kg"}, (
        f"a weight column is missing from one side: {sorted(shapes)}"
    )
    for column, seen in shapes.items():
        assert len(seen) == 1, f"{column} differs between purchase_lines and sales_lines: {seen}"
