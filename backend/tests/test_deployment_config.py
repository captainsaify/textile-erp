"""Deployment manifests -- docs/16_Deployment.md.

These don't build or run anything (no Docker in CI here); they check the
manifests against the code they point at, which is where the mismatches
actually happen: a renamed ASGI path or a queue that no worker consumes
is invisible until deploy time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

DOCKER = Path(__file__).resolve().parents[2] / "docker"


def _load(name: str) -> dict[str, Any]:
    # compose interpolates ${VAR} at runtime; for a structural parse the
    # placeholders just need to survive as plain strings
    loaded: dict[str, Any] = yaml.safe_load((DOCKER / name).read_text())
    return loaded


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return _load("docker-compose.yml")


def test_compose_parses_and_defines_every_documented_service(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    for name in (
        "nginx",
        "api",
        "migrate",
        "worker-whatsapp",
        "worker-ocr",
        "worker-scheduled",
        "beat",
        "postgres",
        "redis",
    ):
        assert name in services, f"{name} missing from docker-compose.yml"


def test_api_entrypoint_matches_the_actual_asgi_app() -> None:
    """`backend.main:app`, not `backend.api.main:app` -- a wrong path
    here fails only when the container starts."""
    dockerfile = (DOCKER / "Dockerfile.api").read_text()
    match = re.search(r'CMD \["uvicorn", "([^"]+)"', dockerfile)
    assert match is not None
    module, _, attribute = match.group(1).partition(":")

    import importlib

    imported = importlib.import_module(module)
    assert hasattr(imported, attribute)


def test_worker_commands_reference_the_real_celery_app() -> None:
    services = _load("docker-compose.yml")["services"]
    import importlib

    for name, service in services.items():
        command = service.get("command")
        if not command or "celery" not in command:
            continue
        app_path = command[command.index("-A") + 1]
        module, _, attribute = app_path.partition(".app")
        imported = importlib.import_module(f"{module}.app")
        assert hasattr(imported, "celery_app"), f"{name} points at a missing Celery app"


def test_every_queue_a_worker_serves_is_one_tasks_actually_use() -> None:
    """A queue nothing consumes silently swallows work; a queue nothing
    produces is a container burning memory for nothing."""
    services = _load("docker-compose.yml")["services"]
    served: set[str] = set()
    for service in services.values():
        command = service.get("command")
        if not command or "-Q" not in (command or []):
            continue
        served.update(command[command.index("-Q") + 1].split(","))

    from backend.workers.schedule import CELERYBEAT_SCHEDULE

    scheduled_queues = {
        entry["options"]["queue"] for entry in CELERYBEAT_SCHEDULE.values() if "options" in entry
    }
    assert scheduled_queues <= served, f"unserved queues: {scheduled_queues - served}"
    # the two dispatched from request handlers rather than Beat
    assert {"reports", "whatsapp", "ocr"} <= served


def test_datastores_are_not_published_in_the_production_manifest(
    compose: dict[str, Any],
) -> None:
    """Postgres and Redis stay inside the compose network; the local
    override adds host ports back for psql access."""
    services = compose["services"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]

    override = _load("docker-compose.override.local.yml")
    assert "ports" in override["services"]["postgres"]


def test_nginx_terminates_tls_and_redirects_plain_http() -> None:
    conf = (DOCKER / "nginx.conf").read_text()
    assert "listen 443 ssl" in conf
    assert "return 301 https://" in conf
    assert "Strict-Transport-Security" in conf
    # ACME must stay reachable over plain HTTP or renewal breaks
    assert ".well-known/acme-challenge" in conf


def test_nginx_body_limit_leaves_room_for_scanned_invoices() -> None:
    conf = (DOCKER / "nginx.conf").read_text()
    match = re.search(r"client_max_body_size\s+(\d+)m", conf)
    assert match is not None
    from backend.core.config import Settings

    limit = Settings.model_fields["max_attachment_size_mb"].default
    assert int(match.group(1)) >= limit, "nginx would reject uploads the app accepts"


def test_containers_do_not_run_as_root() -> None:
    for name in ("Dockerfile.api", "Dockerfile.worker"):
        text = (DOCKER / name).read_text()
        assert "USER appuser" in text, f"{name} runs as root"


def test_only_the_worker_image_carries_ocr_system_libraries() -> None:
    """OCR runs in a worker; the internet-facing API has no reason to
    ship OpenCV's dependencies."""
    api = (DOCKER / "Dockerfile.api").read_text()
    worker = (DOCKER / "Dockerfile.worker").read_text()
    assert "tesseract-ocr" in worker
    assert "tesseract-ocr" not in api
