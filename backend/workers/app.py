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
from celery.signals import worker_process_init, worker_process_shutdown

from backend.core.config import get_settings
from backend.core.observability import configure_tracing, shutdown_tracing
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


# AgentOps exports spans from a background thread, and a thread does not
# survive a fork. The prefork pool imports this module in the parent and
# then forks its children, so initialising at import time would leave
# every child holding an exporter that never runs -- traces would be
# recorded and silently never sent. Each child starts its own here.
@worker_process_init.connect
def _start_tracing(**_: Any) -> None:
    configure_tracing("worker")


@worker_process_shutdown.connect
def _stop_tracing(**_: Any) -> None:
    shutdown_tracing()


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
    """Every registered singleton, not a list maintained here.

    A hand-written list is what let this recur: the engine and Redis
    were released, the WhatsApp client was not, so reports generated
    and then failed to be delivered. See backend/core/lifecycle.py.
    """
    # imported for their @on_release registrations, which only happen
    # when the module is loaded
    import backend.core.db  # noqa: F401
    import backend.core.redis  # noqa: F401
    import backend.services.whatsapp_bridge_client  # noqa: F401
    import backend.services.whatsapp_client  # noqa: F401
    from backend.core.lifecycle import release_all

    await release_all()
