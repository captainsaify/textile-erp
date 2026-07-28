"""Async Redis client -- dedup, rate limiting, session cache, Celery broker.

Lazy singleton, same rationale as core/db.py: importing must never
require config, and tests must be able to swap the URL before first use.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from backend.core.config import get_settings
from backend.core.lifecycle import on_release

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        # redis 6.4 ships from_url without inline types; the client it
        # returns is typed, so this is a gap in the library not here
        _client = aioredis.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url, decode_responses=True
        )
    return _client


@on_release
async def close_redis() -> None:
    """Cleared even if closing fails, for the same reason as
    dispose_engine: a dead client must not be handed to the next
    caller."""
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()
