"""AgentOps tracing stays out of the way.

The point of every test here is the same one: tracing is optional
telemetry wrapped around the most valuable operation in the system, and
it must never change what that operation does. An unconfigured
deployment, a broken SDK and a failing export all have to look identical
to the caller.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from backend.core import observability
from backend.core.config import get_settings


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """`configure_tracing` is once-per-process by design, so each test
    has to start from an un-configured module."""
    observability._enabled = False
    observability._configured = False
    yield
    observability._enabled = False
    observability._configured = False


class _FakeAgentops:
    """Stands in for the SDK, recording what it was asked to do."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.started: list[tuple[str, dict[str, str]]] = []
        self.ended: list[tuple[object, object]] = []
        self.shutdowns = 0
        self.TraceState = type("TraceState", (), {"SUCCESS": "Success", "ERROR": "Error"})
        self.tracer = self

    def start_trace(self, trace_name: str, tags: dict[str, str] | None = None) -> object:
        if self.fail_on == "start":
            raise RuntimeError("exporter unreachable")
        self.started.append((trace_name, tags or {}))
        return object()

    def end_trace(self, context: object, end_state: object = None) -> None:
        if self.fail_on == "end":
            raise RuntimeError("exporter unreachable")
        self.ended.append((context, end_state))

    def shutdown(self) -> None:
        self.shutdowns += 1


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeAgentops) -> _FakeAgentops:
    """Make `import agentops` inside observability resolve to the fake."""
    monkeypatch.setitem(__import__("sys").modules, "agentops", fake)
    return fake


def _with_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    settings = get_settings().model_copy(update={"agentops_api_key": key})
    monkeypatch.setattr(observability, "get_settings", lambda: settings)


def test_no_api_key_leaves_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default deployment. Nothing is imported, nothing is sent."""
    _with_key(monkeypatch, "")
    assert observability.configure_tracing("api") is False
    assert observability._enabled is False


def test_configure_is_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeAgentops())
    calls: list[dict[str, Any]] = []
    fake.init = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    _with_key(monkeypatch, "key-1")

    assert observability.configure_tracing("worker") is True
    assert observability.configure_tracing("worker") is True
    assert len(calls) == 1

    # the settings that keep telemetry from becoming a liability
    assert calls[0]["fail_safe"] is True
    assert calls[0]["auto_start_session"] is False
    assert "component:worker" in calls[0]["default_tags"]


def test_sdk_failure_at_startup_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead AgentOps must not stop the API from booting."""
    fake = _install(monkeypatch, _FakeAgentops())

    def _explode(**_: Any) -> None:
        raise RuntimeError("bad api key")

    fake.init = _explode  # type: ignore[attr-defined]
    _with_key(monkeypatch, "key-1")

    assert observability.configure_tracing("api") is False
    assert observability._enabled is False


def test_trace_is_transparent_when_disabled() -> None:
    entered = False
    with observability.trace("purchase_sheet_read", model="claude-haiku-4-5"):
        entered = True
    assert entered

    with pytest.raises(ValueError, match="boom"), observability.trace("purchase_sheet_read"):
        raise ValueError("boom")


def test_trace_records_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeAgentops())
    fake.init = lambda **_: None  # type: ignore[attr-defined]
    _with_key(monkeypatch, "key-1")
    observability.configure_tracing("worker")

    with observability.trace("purchase_sheet_read", model="claude-haiku-4-5"):
        pass
    assert fake.started == [("purchase_sheet_read", {"model": "claude-haiku-4-5"})]
    assert fake.ended[0][1] == "Success"

    with pytest.raises(ValueError), observability.trace("sale_sheet_read"):
        raise ValueError("the model refused")
    # the failed read is still a trace, marked as one -- a sheet that
    # errored is exactly the one worth opening
    assert fake.ended[1][1] == "Error"


@pytest.mark.parametrize("fail_on", ["start", "end"])
def test_export_failure_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch, fail_on: str
) -> None:
    fake = _install(monkeypatch, _FakeAgentops(fail_on=fail_on))
    fake.init = lambda **_: None  # type: ignore[attr-defined]
    _with_key(monkeypatch, "key-1")
    observability.configure_tracing("worker")

    result = "untouched"
    with observability.trace("purchase_sheet_read"):
        result = "the sheet was read"
    assert result == "the sheet was read"


def test_shutdown_flushes_once_and_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeAgentops())
    fake.init = lambda **_: None  # type: ignore[attr-defined]
    _with_key(monkeypatch, "key-1")

    observability.shutdown_tracing()
    assert fake.shutdowns == 0  # never configured; nothing to flush

    observability.configure_tracing("api")
    observability.shutdown_tracing()
    observability.shutdown_tracing()
    assert fake.shutdowns == 1
