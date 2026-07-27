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

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docker"


def _load(name: str) -> dict[str, Any]:
    # compose interpolates placeholders at runtime; for a structural
    # parse they just need to survive as plain strings
    loaded: dict[str, Any] = yaml.safe_load((ROOT / name).read_text())
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


def test_compose_lives_where_its_env_file_is() -> None:
    """Compose reads `.env` for interpolation from the directory holding
    the compose file. With it under docker/ every ${...} resolved empty
    and Postgres would have started with no username — a failure that
    only shows on first boot."""
    assert (ROOT / "docker-compose.yml").exists()
    assert not (DOCKER / "docker-compose.yml").exists()
    assert (ROOT / ".env.example").exists()


def test_every_variable_compose_interpolates_is_in_the_env_template() -> None:
    text = (ROOT / "docker-compose.yml").read_text() + (
        ROOT / "docker-compose.override.local.yml"
    ).read_text()
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", body))
    template = {
        line.split("=", 1)[0].strip()
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert referenced, "expected compose to interpolate at least the datastore credentials"
    assert referenced <= template, f"not in .env.example: {sorted(referenced - template)}"


def test_env_template_documents_a_value_for_the_datastore_secrets() -> None:
    """An empty POSTGRES_PASSWORD makes the postgres image refuse to
    start, and redis --requirepass '' is invalid."""
    values = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "POSTGRES_USER", "POSTGRES_DB"):
        assert values.get(key), f"{key} has no example value"


def test_build_context_excludes_secrets_and_local_state() -> None:
    """The context is the repo root, so without .dockerignore every
    build would ship .env and .venv to the daemon."""
    ignored = (ROOT / ".dockerignore").read_text()
    for pattern in (".env", ".venv", ".git", "data/"):
        assert pattern in ignored, f"{pattern} not excluded from the build context"


def test_nginx_body_limit_leaves_room_for_scanned_invoices() -> None:
    conf = (DOCKER / "nginx.conf").read_text()
    match = re.search(r"client_max_body_size\s+(\d+)m", conf)
    assert match is not None
    from backend.core.config import Settings

    limit = Settings.model_fields["max_attachment_size_mb"].default
    assert int(match.group(1)) >= limit, "nginx would reject uploads the app accepts"


def test_beat_writes_its_schedule_outside_the_read_only_app_dir() -> None:
    """/app is root-owned on purpose — the app must not be able to
    rewrite its own code — but Celery Beat persists a dbm schedule file
    to its working directory by default, which made it crash-loop with
    EACCES. It has to be pointed at a writable volume."""
    services = _load("docker-compose.yml")["services"]
    command = services["beat"]["command"]
    schedule = next((arg for arg in command if arg.startswith("--schedule=")), None)
    assert schedule is not None, "beat must set --schedule explicitly"
    path = schedule.split("=", 1)[1]
    assert not path.startswith("/app"), "beat's schedule file must not live under /app"

    mounts = [m.split(":")[1] for m in services["beat"].get("volumes", [])]
    assert any(path.startswith(m) for m in mounts), f"{path} is not on a mounted volume"


def test_data_dir_is_writable_by_the_app_user_in_both_images() -> None:
    for name in ("Dockerfile.api", "Dockerfile.worker"):
        text = (DOCKER / name).read_text()
        assert "/data/celery" in text, f"{name} does not create beat's state dir"
        assert "chown -R appuser:appuser /data" in text


def test_tls_material_is_not_committed() -> None:
    ignored = (ROOT / ".gitignore").read_text()
    assert "docker/certs/" in ignored, "nginx private keys would be committed"


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
