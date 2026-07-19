from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from database.mongo import get_database

RUNS = "broadcast_queue_runs"
RECIPIENTS = "broadcast_queue_recipients"
FINAL_RECIPIENT_STATUSES = {"sent", "failed", "blocked", "skipped"}
ACTIVE_RUN_STATUSES = ["pending", "processing"]
MAX_RECIPIENT_ATTEMPTS = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _col(name: str):
    return get_database()[name]


async def initialize_broadcast_indexes() -> None:
    await _col(RUNS).create_index("broadcast_id", unique=True)
    await _col(RUNS).create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
    await _col(RUNS).create_index([("owner_id", ASCENDING), ("created_at", DESCENDING)])
    # Only one unfinished broadcast per owner. The partial index does not affect
    # completed/cancelled history records.
    await _col(RUNS).create_index(
        [("owner_id", ASCENDING)],
        unique=True,
        name="one_active_broadcast_per_owner",
        partialFilterExpression={"status": {"$in": ACTIVE_RUN_STATUSES}},
    )
    await _col(RECIPIENTS).create_index(
        [("broadcast_id", ASCENDING), ("user_id", ASCENDING)], unique=True
    )
    await _col(RECIPIENTS).create_index(
        [("broadcast_id", ASCENDING), ("status", ASCENDING), ("next_retry_at", ASCENDING)]
    )


async def get_active_run_for_owner(owner_id: int) -> dict | None:
    return await _col(RUNS).find_one(
        {"owner_id": int(owner_id), "status": {"$in": ACTIVE_RUN_STATUSES}},
        sort=[("created_at", DESCENDING)],
    )


async def get_latest_run_for_owner(owner_id: int) -> dict | None:
    return await _col(RUNS).find_one(
        {"owner_id": int(owner_id)},
        sort=[("created_at", DESCENDING)],
    )


async def create_run(
    owner_id: int,
    source_chat_id: int,
    source_message_id: int,
    user_ids: list[int],
) -> dict:
    existing = await get_active_run_for_owner(owner_id)
    if existing:
        return existing

    now = _now()
    broadcast_id = uuid4().hex
    clean_user_ids = sorted({int(x) for x in user_ids if isinstance(x, int)})
    run = {
        "broadcast_id": broadcast_id,
        "owner_id": int(owner_id),
        "source_chat_id": int(source_chat_id),
        "source_message_id": int(source_message_id),
        "status": "pending",
        "total": len(clean_user_ids),
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "blocked": 0,
        "skipped": 0,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await _col(RUNS).insert_one(run)
    except DuplicateKeyError:
        existing = await get_active_run_for_owner(owner_id)
        if existing:
            return existing
        raise

    if clean_user_ids:
        await _col(RECIPIENTS).insert_many(
            [
                {
                    "broadcast_id": broadcast_id,
                    "user_id": uid,
                    "status": "pending",
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                }
                for uid in clean_user_ids
            ],
            ordered=False,
        )
    return run


async def claim_run(broadcast_id: str, lease_seconds: int = 120) -> dict | None:
    now = _now()
    claim_token = uuid4().hex
    return await _col(RUNS).find_one_and_update(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ACTIVE_RUN_STATUSES},
            "$or": [
                {"run_lease_until": {"$exists": False}},
                {"run_lease_until": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "status": "processing",
                "run_claim_token": claim_token,
                "run_lease_until": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


async def renew_run_lease(
    broadcast_id: str, claim_token: str, lease_seconds: int = 120
) -> bool:
    now = _now()
    result = await _col(RUNS).update_one(
        {
            "broadcast_id": broadcast_id,
            "status": "processing",
            "run_claim_token": claim_token,
        },
        {
            "$set": {
                "run_lease_until": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


async def claim_recipient(
    broadcast_id: str, max_attempts: int = MAX_RECIPIENT_ATTEMPTS
) -> dict | None:
    now = _now()
    claim_token = uuid4().hex
    return await _col(RECIPIENTS).find_one_and_update(
        {
            "broadcast_id": broadcast_id,
            "attempts": {"$lt": max_attempts},
            "$or": [
                {"status": "pending"},
                {
                    "status": "retry",
                    "$or": [
                        {"next_retry_at": {"$exists": False}},
                        {"next_retry_at": {"$lte": now}},
                    ],
                },
                {
                    "status": "processing",
                    "$or": [
                        {"lease_until": {"$exists": False}},
                        {"lease_until": {"$lte": now}},
                    ],
                },
            ],
        },
        {
            "$set": {
                "status": "processing",
                "claim_token": claim_token,
                "lease_until": now + timedelta(seconds=90),
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("user_id", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


async def finish_recipient(
    broadcast_id: str,
    user_id: int,
    claim_token: str,
    status: str,
    error: str | None = None,
    retry_after: int | None = None,
    max_attempts: int = MAX_RECIPIENT_ATTEMPTS,
) -> str | None:
    now = _now()
    recipient = await _col(RECIPIENTS).find_one(
        {
            "broadcast_id": broadcast_id,
            "user_id": int(user_id),
            "status": "processing",
            "claim_token": claim_token,
        },
        {"attempts": 1},
    )
    if not recipient:
        return None

    final_status = status
    if status == "retry" and int(recipient.get("attempts", 0)) >= max_attempts:
        final_status = "failed"
        error = f"Retry limit reached: {error or 'temporary delivery error'}"
        retry_after = None

    set_values: dict = {"status": final_status, "updated_at": now}
    if error:
        set_values["last_error"] = str(error)[:500]
    if final_status == "retry":
        set_values["next_retry_at"] = now + timedelta(seconds=max(1, retry_after or 5))

    update: dict = {
        "$set": set_values,
        "$unset": {"lease_until": "", "claim_token": ""},
    }
    if final_status != "retry":
        update["$unset"]["next_retry_at"] = ""

    result = await _col(RECIPIENTS).update_one(
        {
            "broadcast_id": broadcast_id,
            "user_id": int(user_id),
            "status": "processing",
            "claim_token": claim_token,
        },
        update,
    )
    return final_status if result.modified_count == 1 else None


async def fail_exhausted_recipients(
    broadcast_id: str, max_attempts: int = MAX_RECIPIENT_ATTEMPTS
) -> int:
    now = _now()
    result = await _col(RECIPIENTS).update_many(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ["pending", "retry", "processing"]},
            "attempts": {"$gte": max_attempts},
            "$or": [
                {"status": {"$ne": "processing"}},
                {"lease_until": {"$exists": False}},
                {"lease_until": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "status": "failed",
                "last_error": "Retry limit reached",
                "updated_at": now,
            },
            "$unset": {
                "lease_until": "",
                "claim_token": "",
                "next_retry_at": "",
            },
        },
    )
    return result.modified_count


async def refresh_run_stats(broadcast_id: str) -> dict:
    pipeline = [
        {"$match": {"broadcast_id": broadcast_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    counts = {row["_id"]: row["count"] async for row in _col(RECIPIENTS).aggregate(pipeline)}
    stats = {
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
        "blocked": counts.get("blocked", 0),
        "skipped": counts.get("skipped", 0),
    }
    stats["processed"] = sum(stats.values())
    await _col(RUNS).update_one(
        {"broadcast_id": broadcast_id},
        {"$set": {**stats, "updated_at": _now()}},
    )
    return stats


async def finalize_if_done(broadcast_id: str) -> bool:
    await fail_exhausted_recipients(broadcast_id)
    remaining = await _col(RECIPIENTS).count_documents(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ["pending", "processing", "retry"]},
        }
    )
    if remaining:
        return False

    stats = await refresh_run_stats(broadcast_id)
    now = _now()
    await _col(RUNS).update_one(
        {"broadcast_id": broadcast_id, "status": {"$ne": "cancelled"}},
        {
            "$set": {
                "status": "completed",
                "finished_at": now,
                "updated_at": now,
                **stats,
            },
            "$unset": {"run_lease_until": "", "run_claim_token": ""},
        },
    )
    return True


async def request_cancel(broadcast_id: str, owner_id: int) -> bool:
    now = _now()
    result = await _col(RUNS).update_one(
        {
            "broadcast_id": broadcast_id,
            "owner_id": int(owner_id),
            "status": {"$in": ACTIVE_RUN_STATUSES},
        },
        {
            "$set": {
                "status": "cancelled",
                "updated_at": now,
                "finished_at": now,
            },
            "$unset": {"run_lease_until": "", "run_claim_token": ""},
        },
    )
    if result.modified_count != 1:
        return False

    await _col(RECIPIENTS).update_many(
        {
            "broadcast_id": broadcast_id,
            "status": {"$in": ["pending", "processing", "retry"]},
        },
        {
            "$set": {
                "status": "skipped",
                "last_error": "Broadcast cancelled by admin",
                "updated_at": now,
            },
            "$unset": {
                "lease_until": "",
                "claim_token": "",
                "next_retry_at": "",
            },
        },
    )
    await refresh_run_stats(broadcast_id)
    return True


async def get_run(broadcast_id: str) -> dict | None:
    return await _col(RUNS).find_one({"broadcast_id": broadcast_id})


async def recoverable_runs(limit: int = 20) -> list[dict]:
    now = _now()
    cursor = (
        _col(RUNS)
        .find(
            {
                "status": {"$in": ACTIVE_RUN_STATUSES},
                "$or": [
                    {"run_lease_until": {"$exists": False}},
                    {"run_lease_until": {"$lte": now}},
                ],
            }
        )
        .sort("created_at", ASCENDING)
        .limit(limit)
    )
    return [doc async for doc in cursor]
