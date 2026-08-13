"""AgentOps tracing for the vision reader (docs/07_OCR.md §5b).

Why it exists: reading a sheet with Claude is the only paid,
non-deterministic step in the system, and the only visibility into it
today is a single `vision_sheet_read` log line carrying token counts.
AgentOps records the same calls as traces -- prompt, model, tokens,
latency, and the transcription that came back -- so a sheet that read
badly can be opened next to one that read well instead of being
re-photographed and guessed at.

It is off unless `AGENTOPS_API_KEY` is set, so a deployment that never
configures it behaves exactly as before.

**It must never be able to take the ERP down.** Telemetry for an OCR
read is worth far less than the purchase that read belongs to, so every
entry point here swallows its own failures, and `fail_safe=True` tells
the SDK to do the same for the instrumentation it installs.

What leaves the machine: the prompts, and the model's answer -- which is
the transcribed sheet, so supplier names, invoice numbers, product codes
and amounts. The sheet photo does not: AgentOps records only the `text`
blocks of a message, and the image is a separate block it skips.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

#: Init is process-wide and not idempotent in a useful way -- the Celery
#: prefork model imports this module once in the parent and then forks,
#: so each child has to arrive here on its own. The lock guards the
#: gunicorn case, where threads in one process can race.
_lock = threading.Lock()
_enabled = False
_configured = False


def _preload_provider_sdks() -> None:
    """Finish importing the provider SDKs before AgentOps goes looking.

    AgentOps installs itself by replacing `builtins.__import__` so it can
    spot provider SDKs as they load. If `anthropic` first loads *after*
    that hook is in place, the hook re-enters the anthropic package while
    it is still initialising, and anthropic's own circular imports break:
    `cannot import name 'BetaToolRunner' from partially initialized
    module 'anthropic.lib.tools'`.

    AgentOps swallows that, logs `Failed to instrument
    AnthropicInstrumentor` in among the startup noise, and carries on
    with **no LLM instrumentation at all** -- traces still arrive, but
    empty of the prompts, tokens and latency that are the whole reason
    for having them. It looks like it is working.

    Importing here first leaves a finished module in `sys.modules` for
    the hook to wrap.
    """
    import anthropic  # noqa: F401


def configure_tracing(component: str) -> bool:
    """Start AgentOps for this process. Returns whether it is on.

    `component` separates the traces of the API process from the
    worker's, which otherwise arrive interleaved with no way to tell
    which produced them.

    Safe to call more than once; only the first call does anything.
    """
    global _enabled, _configured

    with _lock:
        if _configured:
            return _enabled
        _configured = True

        settings = get_settings()
        if not settings.agentops_api_key:
            logger.info("agentops_disabled", reason="no api key configured")
            return False

        try:
            # Must happen before agentops is imported -- see
            # _preload_provider_sdks. It is a call rather than a bare
            # import so that `ruff --fix` cannot sort it below agentops
            # and quietly switch the instrumentation back off.
            _preload_provider_sdks()

            import agentops

            agentops.init(
                api_key=settings.agentops_api_key,
                default_tags=[f"env:{settings.environment}", f"component:{component}"],
                # Each sheet read opens its own trace (see `trace` below).
                # A session started here instead would span the whole life
                # of the process -- weeks, for a worker -- and put every
                # sheet ever read into one unreadable trace.
                auto_start_session=False,
                # Never raise out of the SDK. A telemetry bug must not be
                # able to fail a purchase.
                fail_safe=True,
                # This is a server, not a notebook; the replay URL per
                # trace is noise in the JSON log.
                log_session_replay_url=False,
            )
        except Exception as exc:  # noqa: BLE001 -- telemetry never blocks startup
            logger.warning("agentops_init_failed", component=component, error=str(exc))
            return False

        _enabled = True
        logger.info("agentops_enabled", component=component, environment=settings.environment)
        return True


@contextlib.contextmanager
def trace(name: str, **tags: str) -> Iterator[None]:
    """One trace around one unit of work, with the model calls inside it.

    A no-op when AgentOps is off or unavailable, and it re-raises
    whatever the body raised either way -- the caller's error handling
    is unchanged by whether telemetry happens to be configured.
    """
    if not _enabled:
        yield
        return

    import agentops

    context: Any = None
    try:
        context = agentops.start_trace(trace_name=name, tags=dict(tags))
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentops_trace_start_failed", trace=name, error=str(exc))

    try:
        yield
    except BaseException:
        _end(context, name, success=False)
        raise
    _end(context, name, success=True)


def _end(context: Any, name: str, *, success: bool) -> None:
    if context is None:
        return
    import agentops

    state = agentops.TraceState.SUCCESS if success else agentops.TraceState.ERROR
    try:
        agentops.end_trace(context, end_state=state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentops_trace_end_failed", trace=name, error=str(exc))


def shutdown_tracing() -> None:
    """Flush anything still queued, at process shutdown.

    Spans are batched and exported on an interval, so a process that
    exits promptly after a sheet read would otherwise drop that read's
    trace -- which is precisely the read someone is trying to look at
    after a restart.
    """
    global _enabled
    if not _enabled:
        return
    try:
        import agentops

        agentops.tracer.shutdown()
    except Exception as exc:  # noqa: BLE001 -- shutdown must not raise
        logger.warning("agentops_shutdown_failed", error=str(exc))
    _enabled = False
