from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ASCENDING, ReturnDocument

from database.mongo import get_database

RUNS = "broadcast_queue_runs"
RECIPIENTS = "broadcast_queue_recipients"


def _now():
    return datetime.now(timezone.utc)


def _col(name: str):
    return get_database()[name]


async def initialize_broadcast_indexes() -> None:
    await _col(RUNS).create_index("broadcast_id", unique=True)
    await _col(RUNS).create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
    await _col(RECIPIENTS).create_index([("broadcast_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
    await _col(RECIPIENTS).create_index([("broadcast_id", ASCENDING), ("status", ASCENDING), ("next_retry_at", ASCENDING)])


async def create_run(owner_id: int, source_chat_id: int, source_message_id: int, user_ids: list[int]) -> dict:
    now = _now()
    broadcast_id = uuid4().hex
    user_ids = sorted({int(x) for x in user_ids if isinstance(x, int)})
    run = {
        "broadcast_id": broadcast_id,
        "owner_id": int(owner_id),
        "source_chat_id": int(source_chat_id),
        "source_message_id": int(source_message_id),
        "status": "pending",
        "total": len(user_ids),
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "blocked": 0,
        "skipped": 0,
        "created_at": now,
        "updated_at": now,
    }
    await _col(RUNS).insert_one(run)
    if user_ids:
        await _col(RECIPIENTS).insert_many([
            {"broadcast_id": broadcast_id, "user_id": uid, "status": "pending", "attempts": 0, "created_at": now, "updated_at": now}
            for uid in user_ids
        ], ordered=False)
    return run


async def claim_run(broadcast_id: str, lease_seconds: int = 120) -> dict | None:
    now = _now()
    return await _col(RUNS).find_one_and_update(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ["pending", "processing"]},
            "$or": [
                {"lease_until": {"$exists": False}},
                {"lease_until": {"$lte": now}},
            ],
        },
        {"$set": {"status": "processing", "lease_until": now + timedelta(seconds=lease_seconds), "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )


async def renew_run_lease(broadcast_id: str, lease_seconds: int = 120) -> None:
    now = _now()
    await _col(RUNS).update_one({"broadcast_id": broadcast_id, "status": "processing"}, {"$set": {"lease_until": now + timedelta(seconds=lease_seconds), "updated_at": now}})


async def claim_recipient(broadcast_id: str, max_attempts: int = 4) -> dict | None:
    now = _now()
    return await _col(RECIPIENTS).find_one_and_update(
        {
            "broadcast_id": broadcast_id,
            "attempts": {"$lt": max_attempts},
            "$or": [
                {"status": "pending"},
                {"status": "retry", "next_retry_at": {"$lte": now}},
                {"status": "processing", "lease_until": {"$lte": now}},
            ],
        },
        {"$set": {"status": "processing", "lease_until": now + timedelta(seconds=90), "updated_at": now}, "$inc": {"attempts": 1}},
        sort=[("user_id", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


async def finish_recipient(broadcast_id: str, user_id: int, status: str, error: str | None = None, retry_after: int | None = None) -> None:
    now = _now()
    update: dict = {"$set": {"status": status, "updated_at": now}, "$unset": {"lease_until": ""}}
    if error:
        update["$set"]["last_error"] = error[:500]
    if retry_after is not None:
        update["$set"]["next_retry_at"] = now + timedelta(seconds=max(1, retry_after))
    await _col(RECIPIENTS).update_one({"broadcast_id": broadcast_id, "user_id": int(user_id)}, update)


async def refresh_run_stats(broadcast_id: str) -> dict:
    pipeline = [{"$match": {"broadcast_id": broadcast_id}}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    counts = {row["_id"]: row["count"] async for row in _col(RECIPIENTS).aggregate(pipeline)}
    final = {"sent", "failed", "blocked", "skipped"}
    stats = {
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
        "blocked": counts.get("blocked", 0),
        "skipped": counts.get("skipped", 0),
    }
    stats["processed"] = sum(stats[k] for k in final)
    await _col(RUNS).update_one({"broadcast_id": broadcast_id}, {"$set": {**stats, "updated_at": _now()}})
    return stats


async def finalize_if_done(broadcast_id: str) -> bool:
    remaining = await _col(RECIPIENTS).count_documents({"broadcast_id": broadcast_id, "status": {"$in": ["pending", "processing", "retry"]}})
    if remaining:
        return False
    stats = await refresh_run_stats(broadcast_id)
    await _col(RUNS).update_one({"broadcast_id": broadcast_id, "status": {"$ne": "cancelled"}}, {"$set": {"status": "completed", "finished_at": _now(), "updated_at": _now(), **stats}, "$unset": {"lease_until": ""}})
    return True


async def request_cancel(broadcast_id: str, owner_id: int) -> bool:
    now = _now()
    result = await _col(RUNS).update_one(
        {
            "broadcast_id": broadcast_id,
            "owner_id": int(owner_id),
            "status": {"$in": ["pending", "processing"]},
        },
        {
            "$set": {"status": "cancelled", "updated_at": now, "finished_at": now},
            "$unset": {"lease_until": ""},
        },
    )
    if result.modified_count != 1:
        return False

    # Make unfinished recipient rows terminal so status counts remain truthful.
    await _col(RECIPIENTS).update_many(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ["pending", "processing", "retry"]},
        },
        {
            "$set": {"status": "skipped", "updated_at": now, "last_error": "Broadcast cancelled by owner."},
            "$unset": {"lease_until": "", "next_retry_at": ""},
        },
    )
    await refresh_run_stats(broadcast_id)
    return True


async def get_latest_run_for_owner(owner_id: int) -> dict | None:
    """Return the owner's newest broadcast, including after a restart."""
    return await _col(RUNS).find_one(
        {"owner_id": int(owner_id)},
        sort=[("created_at", -1)],
    )


async def exhaust_retry_limit(broadcast_id: str, max_attempts: int = 4) -> int:
    """Move recipients that used every retry to a terminal failed state.

    Without this transition a broadcast can remain in processing forever:
    those rows are no longer claimable, but finalize_if_done still sees retry.
    """
    now = _now()
    result = await _col(RECIPIENTS).update_many(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ["retry", "processing"]},
            "attempts": {"$gte": max(1, int(max_attempts))},
            "$or": [
                {"lease_until": {"$exists": False}},
                {"lease_until": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "status": "failed",
                "last_error": "Maximum broadcast delivery attempts reached.",
                "updated_at": now,
            },
            "$unset": {"lease_until": "", "next_retry_at": ""},
        },
    )
    return int(result.modified_count)


async def get_run(broadcast_id: str) -> dict | None:
    return await _col(RUNS).find_one({"broadcast_id": broadcast_id})


async def recoverable_runs(limit: int = 20) -> list[dict]:
    now = _now()
    cursor = _col(RUNS).find({"status": {"$in": ["pending", "processing"]}, "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lte": now}}]}).sort("created_at", ASCENDING).limit(limit)
    return [doc async for doc in cursor]
