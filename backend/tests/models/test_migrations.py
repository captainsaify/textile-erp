"""Migration discipline check from docs/02_Database.md §6: every
migration must survive upgrade -> downgrade -> upgrade against a real
database, seed data included."""

from __future__ import annotations

from backend.tests.conftest import run_alembic


def test_upgrade_downgrade_upgrade_round_trip(migrated_test_db: str) -> None:
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
