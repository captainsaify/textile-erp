"""Celery application -- docs/11_BackgroundWorkers.md §1, §2.

Separate queues so a burst of OCR work can never starve the
latency-sensitive WhatsApp send queue (§2). Tasks run in worker
processes with no event loop of their own, so each one drives its async
service call through `run_async` rather than assuming a running loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from celery import Celery

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.workers.schedule import CELERYBEAT_SCHEDULE

settings = get_settings()

celery_app = Celery(
    "textile_erp",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["backend.workers.tasks"],
)

celery_app.conf.update(
    task_default_queue="scheduled",
    task_acks_late=True,
    # a task killed mid-flight is re-delivered; combined with the
    # idempotence rule in §1 that is safe, and it beats losing the job
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    result_expires=24 * 60 * 60,
)

# Wire the Beat schedule onto the app itself. `celery ... beat` reads
# beat_schedule off the configured app, so a schedule module that is
# merely *defined* and never assigned produces a Beat process that
# starts cleanly and fires nothing -- forever, and silently.
celery_app.conf.beat_schedule = CELERYBEAT_SCHEDULE


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine to completion inside a synchronous Celery task.

    A fresh loop per task, **and the loop-bound singletons released
    before it closes**. The second half is not optional: the SQLAlchemy
    async engine and the Redis client are module-level singletons that
    bind to whichever loop first touches them, and they outlive the
    task. Give the next task a new loop while the engine's pool still
    holds connections belonging to the old one, and it dies on
    `pool_pre_ping` with "got Future attached to a different loop" --
    which is what silently stopped every report after the first one in
    each worker process.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        # Before as well as after: this loop must not inherit a singleton
        # bound to some earlier one, whoever created it.
        loop.run_until_complete(_release_loop_bound_singletons())
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(_release_loop_bound_singletons())
        asyncio.set_event_loop(None)
        loop.close()


async def _release_loop_bound_singletons() -> None:
    """Best-effort: a failure to tidy up must not mask the task's own
    exception, but leaving them bound would break the *next* task."""
    from backend.core.db import dispose_engine
    from backend.core.redis import close_redis

    for release in (dispose_engine, close_redis):
        try:
            await release()
        except Exception as exc:  # noqa: BLE001
            get_logger(__name__).warning("worker_singleton_release_failed", error=str(exc))
