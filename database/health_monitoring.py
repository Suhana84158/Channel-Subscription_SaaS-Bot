from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from database.mongo import get_database

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        logger.warning("Invalid %s value; using %s", name, default)
        return max(minimum, default)


_FAILURE_THRESHOLD = _env_int("HEALTH_FAILURE_THRESHOLD", 3, 1)
_SNAPSHOT_INTERVAL = _env_int("HEALTH_SNAPSHOT_INTERVAL_SECONDS", 60, 30)
_RETENTION_DAYS = _env_int("HEALTH_HISTORY_DAYS", 7, 1)

_lock = asyncio.Lock()
_consecutive_failures: dict[str, int] = defaultdict(int)
_last_snapshot_monotonic: dict[str, float] = defaultdict(float)
_indexes_ready = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_source(source: str) -> str:
    value = str(source or "unknown").strip()[:64]
    return value or "unknown"


async def _ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        db = get_database()
        await db.health_snapshots.create_index(
            "created_at",
            expireAfterSeconds=_RETENTION_DAYS * 86400,
            name="health_snapshot_ttl",
        )
        await db.health_snapshots.create_index(
            [("source", 1), ("created_at", -1)],
            name="health_source_created",
        )
        _indexes_ready = True
    except Exception:
        logger.debug("Health history indexes are not ready", exc_info=True)


async def record_health_snapshot(
    *,
    source: str,
    raw_healthy: bool,
    details: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Apply a failure threshold and persist a throttled health snapshot.

    Failure counters and throttling are maintained independently per source.
    This prevents an owner-dashboard refresh from changing the HTTP monitor's
    state, or vice versa.
    """
    source_key = _safe_source(source)

    async with _lock:
        if raw_healthy:
            _consecutive_failures[source_key] = 0
        else:
            _consecutive_failures[source_key] += 1

        failures = _consecutive_failures[source_key]
        unhealthy = (not raw_healthy) and failures >= _FAILURE_THRESHOLD
        status = "healthy" if raw_healthy else ("unhealthy" if unhealthy else "degraded")
        result = {
            "source": source_key,
            "status": status,
            "raw_healthy": bool(raw_healthy),
            "consecutive_failures": failures,
            "failure_threshold": _FAILURE_THRESHOLD,
        }

        now_mono = time.monotonic()
        should_store = force or (
            now_mono - _last_snapshot_monotonic[source_key] >= _SNAPSHOT_INTERVAL
        )
        if not should_store:
            return result
        _last_snapshot_monotonic[source_key] = now_mono

    try:
        await _ensure_indexes()
        db = get_database()
        await db.health_snapshots.insert_one(
            {
                "source": source_key,
                "status": status,
                "raw_healthy": bool(raw_healthy),
                "consecutive_failures": failures,
                "failure_threshold": _FAILURE_THRESHOLD,
                "details": details or {},
                "created_at": _utcnow(),
            }
        )
    except Exception:
        logger.debug("Unable to store health snapshot", exc_info=True)

    return result


async def get_health_summary(
    hours: int = 24,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    hours = min(max(int(hours), 1), 168)
    since = _utcnow() - timedelta(hours=hours)
    match: dict[str, Any] = {"created_at": {"$gte": since}}
    if source:
        match["source"] = _safe_source(source)

    try:
        await _ensure_indexes()
        db = get_database()
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "healthy": {"$sum": {"$cond": [{"$eq": ["$status", "healthy"]}, 1, 0]}},
                    "degraded": {"$sum": {"$cond": [{"$eq": ["$status", "degraded"]}, 1, 0]}},
                    "unhealthy": {"$sum": {"$cond": [{"$eq": ["$status", "unhealthy"]}, 1, 0]}},
                    "last_checked": {"$max": "$created_at"},
                }
            },
        ]
        rows = await db.health_snapshots.aggregate(pipeline).to_list(length=1)
        if not rows:
            return {
                "source": _safe_source(source) if source else None,
                "total": 0,
                "availability_percent": None,
            }
        row = rows[0]
        total = int(row.get("total", 0) or 0)
        healthy = int(row.get("healthy", 0) or 0)
        # Availability reflects successful checks only; degraded samples remain
        # visible separately instead of being silently counted as healthy.
        availability = round((healthy / total) * 100, 2) if total else None
        return {
            "source": _safe_source(source) if source else None,
            "total": total,
            "healthy": healthy,
            "degraded": int(row.get("degraded", 0) or 0),
            "unhealthy": int(row.get("unhealthy", 0) or 0),
            "availability_percent": availability,
            "last_checked": row.get("last_checked"),
        }
    except Exception:
        logger.debug("Unable to read health history", exc_info=True)
        return {
            "source": _safe_source(source) if source else None,
            "total": 0,
            "availability_percent": None,
        }
