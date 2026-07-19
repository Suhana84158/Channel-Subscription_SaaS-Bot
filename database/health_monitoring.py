from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from database.mongo import get_database

logger = logging.getLogger(__name__)

_FAILURE_THRESHOLD = max(1, int(os.getenv("HEALTH_FAILURE_THRESHOLD", "3")))
_SNAPSHOT_INTERVAL = max(30, int(os.getenv("HEALTH_SNAPSHOT_INTERVAL_SECONDS", "60")))
_RETENTION_DAYS = max(1, int(os.getenv("HEALTH_HISTORY_DAYS", "7")))

_lock = asyncio.Lock()
_consecutive_failures = 0
_last_snapshot_monotonic = 0.0
_indexes_ready = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    Temporary one-off failures remain degraded instead of immediately becoming
    unhealthy. Persistence is best-effort and can never break the health check.
    """
    global _consecutive_failures, _last_snapshot_monotonic

    async with _lock:
        if raw_healthy:
            _consecutive_failures = 0
        else:
            _consecutive_failures += 1

        unhealthy = (not raw_healthy) and _consecutive_failures >= _FAILURE_THRESHOLD
        status = "healthy" if raw_healthy else ("unhealthy" if unhealthy else "degraded")
        result = {
            "status": status,
            "raw_healthy": bool(raw_healthy),
            "consecutive_failures": _consecutive_failures,
            "failure_threshold": _FAILURE_THRESHOLD,
        }

        now_mono = time.monotonic()
        should_store = force or (now_mono - _last_snapshot_monotonic >= _SNAPSHOT_INTERVAL)
        if not should_store:
            return result
        _last_snapshot_monotonic = now_mono

    try:
        await _ensure_indexes()
        db = get_database()
        await db.health_snapshots.insert_one(
            {
                "source": str(source)[:64],
                "status": status,
                "raw_healthy": bool(raw_healthy),
                "consecutive_failures": result["consecutive_failures"],
                "failure_threshold": _FAILURE_THRESHOLD,
                "details": details or {},
                "created_at": _utcnow(),
            }
        )
    except Exception:
        logger.debug("Unable to store health snapshot", exc_info=True)

    return result


async def get_health_summary(hours: int = 24) -> dict[str, Any]:
    hours = min(max(int(hours), 1), 168)
    since = _utcnow() - timedelta(hours=hours)
    try:
        await _ensure_indexes()
        db = get_database()
        pipeline = [
            {"$match": {"created_at": {"$gte": since}}},
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
            return {"total": 0, "availability_percent": None}
        row = rows[0]
        total = int(row.get("total", 0) or 0)
        healthy = int(row.get("healthy", 0) or 0)
        availability = round((healthy / total) * 100, 2) if total else None
        return {
            "total": total,
            "healthy": healthy,
            "degraded": int(row.get("degraded", 0) or 0),
            "unhealthy": int(row.get("unhealthy", 0) or 0),
            "availability_percent": availability,
            "last_checked": row.get("last_checked"),
        }
    except Exception:
        logger.debug("Unable to read health history", exc_info=True)
        return {"total": 0, "availability_percent": None}
