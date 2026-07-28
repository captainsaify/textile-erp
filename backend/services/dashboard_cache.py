"""Dashboard caching -- docs/12_Dashboard.md §4.

Two things made this worth deferring until now, and both are addressed
here rather than hoped away.

**1. A missed invalidation is silent staleness.** The doc asks every
mutating service to invalidate on write, which is a cross-cutting change
across purchase, sale, payment, expense, capital and inventory -- and
one forgotten call shows a stale number for up to a minute with nothing
to indicate it. Instead this hooks the one place every mutation already
goes through: `audit_logs`. `CLAUDE.md` rule 3 makes that an invariant --
"no table holding business data may be changed without a corresponding
audit_logs row" -- so invalidating from `AuditService.record` covers
every write there can be, including ones not written yet.

**2. Read-modify-write staleness.** Deleting a key on write still lets a
read that started *before* the write finish afterwards and store the
pre-write value. So the key carries a version: invalidation bumps the
counter, and a read that computed under version N writes to the key for
version N, which nobody will look at again. A slow read can never
overwrite a newer value; it just wastes a write.

The 60s TTL is what the doc calls it -- a safety net that bounds
staleness if invalidation itself fails -- not the freshness mechanism.

Money survives the round trip as `Decimal`, never float: values are
encoded as strings and decoded back through the dataclass's own type
hints, and a round-trip test pins the whole nested snapshot.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
import types
import typing
import uuid
from typing import Any, get_args, get_origin, get_type_hints

from backend.core.logging import get_logger
from backend.core.redis import get_redis

logger = get_logger(__name__)

TTL_SECONDS = 60

def _version_key(org_id: uuid.UUID) -> str:
    return f"dashboard:ver:{org_id}"


def _payload_key(org_id: uuid.UUID, version: int, variant: str) -> str:
    return f"dashboard:{org_id}:{version}:{variant}"


# --------------------------------------------------------------------
# codec
# --------------------------------------------------------------------


def encode(value: Any) -> Any:
    """Dataclasses to JSON-safe primitives, with Decimal and date kept
    distinguishable from the strings they serialise as."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: encode(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, datetime.date):
        return {"__date__": value.isoformat()}
    if isinstance(value, list):
        return [encode(item) for item in value]
    return value


def decode(target: Any, value: Any) -> Any:
    """Rebuild `target` from what `encode` produced.

    Driven by the dataclass's own type hints, so a field whose type
    changes without this being updated fails loudly at decode rather
    than quietly handing back the wrong type.
    """
    if value is None:
        return None

    origin = get_origin(target)
    if origin in (typing.Union, types.UnionType):
        inner = [arg for arg in get_args(target) if arg is not type(None)]
        return decode(inner[0], value) if inner else value
    if origin is list:
        (item_type,) = get_args(target) or (Any,)
        return [decode(item_type, item) for item in value]
    if target is decimal.Decimal:
        return decimal.Decimal(value["__decimal__"])
    if target is datetime.date:
        return datetime.date.fromisoformat(value["__date__"])
    if dataclasses.is_dataclass(target) and isinstance(target, type):
        hints = get_type_hints(target)
        return target(
            **{
                field.name: decode(hints[field.name], value[field.name])
                for field in dataclasses.fields(target)
            }
        )
    return value


# --------------------------------------------------------------------
# cache
# --------------------------------------------------------------------


async def current_version(org_id: uuid.UUID) -> int:
    raw = await get_redis().get(_version_key(org_id))
    return int(raw) if raw else 0


async def invalidate(org_id: uuid.UUID) -> None:
    """Bump the version so every cached payload becomes unreachable.

    Never raises: a cache that cannot be invalidated must not fail the
    write that triggered it. The TTL bounds the damage.
    """
    try:
        await get_redis().incr(_version_key(org_id))
    except Exception as exc:  # noqa: BLE001 -- cache failure is not a write failure
        logger.warning("dashboard_cache_invalidate_failed", org_id=str(org_id), error=str(exc))


async def load[T](org_id: uuid.UUID, target: type[T], *, variant: str) -> tuple[T | None, int]:
    """Returns (hit or None, the version it was read under).

    The version comes back so the caller can store under the same one --
    that is what stops a slow recomputation overwriting a fresher value.
    """
    try:
        version = await current_version(org_id)
        raw = await get_redis().get(_payload_key(org_id, version, variant))
    except Exception as exc:  # noqa: BLE001 -- degrade to computing directly
        logger.warning("dashboard_cache_read_failed", org_id=str(org_id), error=str(exc))
        return None, -1
    if raw is None:
        return None, version
    try:
        return typing.cast("T", decode(target, json.loads(raw))), version
    except Exception as exc:  # noqa: BLE001 -- a bad entry is a miss, not an error
        logger.warning("dashboard_cache_decode_failed", org_id=str(org_id), error=str(exc))
        return None, version


async def store(org_id: uuid.UUID, value: Any, *, variant: str, version: int) -> None:
    """Store under the version the read began at. If a write bumped the
    version meanwhile, this lands on a key nobody will read -- which is
    the point."""
    if version < 0:
        return
    try:
        await get_redis().set(
            _payload_key(org_id, version, variant),
            json.dumps(encode(value)),
            ex=TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dashboard_cache_write_failed", org_id=str(org_id), error=str(exc))
