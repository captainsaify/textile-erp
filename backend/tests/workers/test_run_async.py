"""`run_async` -- the bridge from a synchronous Celery task into async
code (docs/11_BackgroundWorkers.md §2).

The bug this pins: every task gets a fresh event loop, but the async
engine and Redis client are module-level singletons that bind to
whichever loop first touched them. Leave them bound and the *second*
task in a worker process dies on pool_pre_ping with "got Future
attached to a different loop" -- which is exactly how reports stopped
generating while the job row sat at `queued` and nobody was told.

One task always worked, so nothing that ran a single task caught it.
"""

from __future__ import annotations

import sqlalchemy as sa

from backend.core.db import get_session_factory
from backend.workers.app import run_async


def _touch_database() -> int:
    async def _run() -> int:
        async with get_session_factory()() as session:
            return int((await session.execute(sa.text("SELECT 1"))).scalar_one())

    return run_async(_run())


def test_consecutive_tasks_each_get_a_working_database() -> None:
    """Two calls, as a long-lived worker makes. The second is the one
    that used to fail."""
    assert _touch_database() == 1
    assert _touch_database() == 1
    assert _touch_database() == 1


def test_a_failing_task_still_releases_its_loop_bound_singletons() -> None:
    """Cleanup runs in `finally`, so a task that raises doesn't poison
    the next one -- otherwise one bad task takes the worker down with it
    until a restart."""

    async def _boom() -> None:
        async with get_session_factory()() as session:
            await session.execute(sa.text("SELECT 1"))
        raise RuntimeError("task failed")

    try:
        run_async(_boom())
    except RuntimeError as exc:
        assert str(exc) == "task failed"
    else:  # pragma: no cover - the coroutine raises by construction
        raise AssertionError("expected the task to raise")

    assert _touch_database() == 1


def test_every_process_wide_singleton_is_registered_for_release() -> None:
    """The guard against a third recurrence.

    This bug landed twice in one day: the database engine (reports
    stopped generating) and then the WhatsApp client (reports generated
    but were never delivered). Both were a module-level async singleton
    that nothing released at a task boundary. Any new one has to be
    registered with @on_release, and this fails until it is.
    """
    import re
    from pathlib import Path

    import backend.core.db  # noqa: F401
    import backend.core.redis  # noqa: F401
    import backend.services.whatsapp_bridge_client  # noqa: F401
    import backend.services.whatsapp_client  # noqa: F401
    from backend.core.lifecycle import registered

    root = Path(__file__).resolve().parents[2]
    # a module-level `_thing: X | None = None` plus a close/dispose
    # coroutine is the shape of a process-wide singleton here
    holder = re.compile(r"^_\w+: .*\| None = None$", re.MULTILINE)
    releaser = re.compile(r"^async def ((?:close|dispose)_\w+)\(\) -> None:", re.MULTILINE)

    expected: set[str] = set()
    for path in (*root.glob("core/*.py"), *root.glob("services/*.py")):
        source = path.read_text()
        if holder.search(source):
            expected.update(releaser.findall(source))

    missing = expected - set(registered())
    assert not missing, (
        f"process-wide singletons not registered with @on_release: {sorted(missing)}. "
        "A Celery task runs in a fresh event loop; anything left bound to the previous "
        "one breaks the next task."
    )
