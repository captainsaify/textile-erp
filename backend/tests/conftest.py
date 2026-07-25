"""Shared fixtures. DB-backed tests run against TEST_DATABASE_URL and are
skipped (not failed) when it isn't configured, so the pure-metadata tests
still run anywhere."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def run_alembic(*args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stderr}")


@pytest.fixture(scope="session")
def migrated_test_db() -> str:
    """Test database migrated to head (idempotent if already there)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    run_alembic("upgrade", "head")
    return TEST_DATABASE_URL
