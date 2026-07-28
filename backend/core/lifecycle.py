"""Process-wide async singletons, and how to let go of them.

Several modules keep a lazily-created client in a module global: the
SQLAlchemy engine, the Redis client, the WhatsApp Cloud API client, the
bridge sender. Each binds to whichever event loop first touches it.

That is fine in the API process, which has one loop for its lifetime.
It is not fine in a Celery worker, where **every task runs in a new
loop** -- a singleton left over from the previous task belongs to a loop
that is already closed, and the next use fails with "got Future attached
to a different loop" or "Event loop is closed".

This bit twice in one day: first the database engine (reports stopped
generating), then the WhatsApp client (reports generated but were never
delivered). Both times the second task in a worker process was the one
that broke, so a single task always looked fine.

Registering here is what stops a third. A module that adds a
process-wide async singleton decorates its close function with
`@on_release`, and every consumer -- worker task boundaries, API
shutdown -- gets it automatically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from backend.core.logging import get_logger

logger = get_logger(__name__)

Releaser = Callable[[], Awaitable[None]]

_RELEASERS: list[Releaser] = []


def on_release(func: Releaser) -> Releaser:
    """Register a singleton's close function. Returns it unchanged, so
    it stays directly callable."""
    _RELEASERS.append(func)
    return func


def registered() -> tuple[str, ...]:
    """Names of everything registered -- used by the test that keeps
    this list honest."""
    return tuple(func.__name__ for func in _RELEASERS)


async def release_all() -> None:
    """Release every registered singleton.

    One failure never prevents the rest: these run at a task boundary or
    during shutdown, where the job is to leave nothing bound to a loop
    that is about to disappear, and a client that refuses to close
    cleanly is the least of the problems.
    """
    for release in _RELEASERS:
        try:
            await release()
        except Exception as exc:  # noqa: BLE001 -- cleanup must not raise
            logger.warning("singleton_release_failed", releaser=release.__name__, error=str(exc))
