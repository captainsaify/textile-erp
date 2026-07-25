"""Async Redis client -- dedup, rate limiting, session cache, Celery broker.

Lazy singleton, same rationale as core/db.py: importing must never
require config, and tests must be able to swap the URL before first use.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from backend.core.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
