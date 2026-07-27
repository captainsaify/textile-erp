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

    A fresh loop per task, closed afterwards: the SQLAlchemy async
    engine and the Redis client both bind to the loop that first touches
    them, so reusing a loop across tasks in a long-lived worker is how
    you end up with "attached to a different loop" errors under load.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
