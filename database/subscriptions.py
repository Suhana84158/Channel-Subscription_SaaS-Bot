import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from uuid import uuid4

from database.mongo import get_database

COLLECTION = "subscriptions"

_user_locks: dict[int, asyncio.Lock] = {}
_user_locks_guard = asyncio.Lock()


def subscriptions_collection():
    return get_database()[COLLECTION]


def make_aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _get_user_lock(user_id: int) -> asyncio.Lock:
    async with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = lock
        return lock


@asynccontextmanager
async def subscription_lock(user_id: int):
    """Serialize renew/expire operations for one user in this runtime."""
    lock = await _get_user_lock(user_id)
    async with lock:
        yield


def _duration_milliseconds(
    duration_days: int = 0,
    duration_minutes: int = 0,
) -> int:
    if duration_minutes > 0:
        return int(duration_minutes) * 60 * 1000
    return int(duration_days) * 24 * 60 * 60 * 1000


async def activate_subscription(
    user_id: int,
    plan_name: str,
    duration_days: int = 0,
    duration_minutes: int = 0,
):
    now = datetime.now(timezone.utc)

    if duration_minutes > 0:
        expiry = now + timedelta(minutes=duration_minutes)
    else:
        expiry = now + timedelta(days=duration_days)

    async with subscription_lock(user_id):
        await subscriptions_collection().update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "plan": plan_name,
                    "active": True,
                    "start_date": now,
                    "expiry_date": expiry,
                    "updated_at": now,
                },
                "$unset": {
                    "expiring": "",
                    "expired_at": "",
                    "expiry_claim_token": "",
                    "expiry_claimed_at": "",
                    "expiry_notification_token": "",
                    "expiry_notification_claimed_at": "",
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    return expiry


async def fulfill_payment_subscription(
    user_id: int,
    fulfillment_key: str,
    plan_name: str,
    duration_days: int = 0,
    duration_minutes: int = 0,
):
    """
    Atomically activate or renew a subscription once per payment key.

    The fulfillment key is checked and recorded in the same MongoDB write that
    changes the expiry date, so retries cannot extend a subscription twice.
    """
    key = str(fulfillment_key).strip()
    if not key:
        raise ValueError("fulfillment_key is required")

    duration_ms = _duration_milliseconds(
        duration_days=duration_days,
        duration_minutes=duration_minutes,
    )
    if duration_ms <= 0:
        raise ValueError("Subscription duration must be greater than zero")

    now = datetime.now(timezone.utc)

    document = await subscriptions_collection().find_one_and_update(
        {"user_id": int(user_id)},
        [
            {
                "$set": {
                    "_fulfillment_keys": {
                        "$ifNull": ["$payment_fulfillment_keys", []]
                    },
                    "_was_active": {
                        "$and": [
                            {"$eq": [{"$ifNull": ["$active", False]}, True]},
                            {"$gt": [{"$ifNull": ["$expiry_date", now]}, now]},
                        ]
                    },
                }
            },
            {
                "$set": {
                    "_already_fulfilled": {
                        "$in": [key, "$_fulfillment_keys"]
                    },
                    "_base_expiry": {
                        "$cond": [
                            "$_was_active",
                            "$expiry_date",
                            now,
                        ]
                    },
                }
            },
            {
                "$set": {
                    "user_id": int(user_id),
                    "plan": {
                        "$cond": [
                            "$_already_fulfilled",
                            {"$ifNull": ["$plan", plan_name]},
                            plan_name,
                        ]
                    },
                    "active": {
                        "$cond": [
                            "$_already_fulfilled",
                            {"$ifNull": ["$active", True]},
                            True,
                        ]
                    },
                    "start_date": {
                        "$cond": [
                            "$_already_fulfilled",
                            {"$ifNull": ["$start_date", now]},
                            {
                                "$cond": [
                                    "$_was_active",
                                    {"$ifNull": ["$start_date", now]},
                                    now,
                                ]
                            },
                        ]
                    },
                    "expiry_date": {
                        "$cond": [
                            "$_already_fulfilled",
                            "$expiry_date",
                            {"$add": ["$_base_expiry", duration_ms]},
                        ]
                    },
                    "payment_fulfillment_keys": {
                        "$cond": [
                            "$_already_fulfilled",
                            "$_fulfillment_keys",
                            {
                                "$concatArrays": [
                                    "$_fulfillment_keys",
                                    [key],
                                ]
                            },
                        ]
                    },
                    "last_fulfillment_key": key,
                    "last_fulfillment_applied": {
                        "$not": ["$_already_fulfilled"]
                    },
                    "last_fulfillment_action": {
                        "$cond": [
                            "$_already_fulfilled",
                            {
                                "$ifNull": [
                                    "$last_fulfillment_action",
                                    "duplicate",
                                ]
                            },
                            {
                                "$cond": [
                                    "$_was_active",
                                    "renewed",
                                    "activated",
                                ]
                            },
                        ]
                    },
                    "last_fulfilled_at": {
                        "$cond": [
                            "$_already_fulfilled",
                            "$last_fulfilled_at",
                            now,
                        ]
                    },
                    "updated_at": {
                        "$cond": [
                            "$_already_fulfilled",
                            {"$ifNull": ["$updated_at", now]},
                            now,
                        ]
                    },
                    "created_at": {"$ifNull": ["$created_at", now]},
                    "expiring": {
                        "$cond": [
                            "$_already_fulfilled",
                            {"$ifNull": ["$expiring", False]},
                            False,
                        ]
                    },
                }
            },
            {
                "$unset": [
                    "_fulfillment_keys",
                    "_was_active",
                    "_already_fulfilled",
                    "_base_expiry",
                    "expired_at",
                ]
            },
        ],
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    if document is None:
        raise RuntimeError("Subscription fulfillment did not return a record")

    return {
        "expiry": make_aware(document.get("expiry_date")),
        "applied": bool(document.get("last_fulfillment_applied")),
        "action": document.get("last_fulfillment_action", "activated"),
    }


async def get_subscription(user_id: int):
    return await subscriptions_collection().find_one({"user_id": user_id})


async def renew_subscription(
    user_id: int,
    duration_days: int = 0,
    duration_minutes: int = 0,
):
    now = datetime.now(timezone.utc)

    if duration_minutes > 0:
        add_time = timedelta(minutes=duration_minutes)
    else:
        add_time = timedelta(days=duration_days)

    async with subscription_lock(user_id):
        subscription = await get_subscription(user_id)
        expiry_date = None
        if subscription:
            expiry_date = make_aware(subscription.get("expiry_date"))

        if expiry_date and expiry_date > now:
            expiry = expiry_date + add_time
        else:
            expiry = now + add_time

        await subscriptions_collection().update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "active": True,
                    "expiry_date": expiry,
                    "updated_at": now,
                },
                "$unset": {
                    "expiring": "",
                    "expired_at": "",
                    "expiry_claim_token": "",
                    "expiry_claimed_at": "",
                    "expiry_notification_token": "",
                    "expiry_notification_claimed_at": "",
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    return expiry


async def expire_subscription(
    user_id: int,
    expected_expiry=None,
) -> bool:
    """Expire only the still-active subscription that was actually processed."""
    query = {
        "user_id": user_id,
        "active": True,
    }

    if expected_expiry is not None:
        query["expiry_date"] = expected_expiry

    now = datetime.now(timezone.utc)
    result = await subscriptions_collection().update_one(
        query,
        {
            "$set": {
                "active": False,
                "expiring": False,
                "expired_at": now,
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


async def claim_expired_subscription(
    user_id: int,
    *,
    now=None,
    stale_after_seconds: int = 900,
):
    """
    Atomically claim an expired active subscription for one worker.

    Stale claims are recoverable after stale_after_seconds. Renewals clear the
    claim fields, so a renewed subscription cannot be expired by an old worker.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stale_before = now - timedelta(
        seconds=max(60, int(stale_after_seconds))
    )
    claim_token = uuid4().hex

    document = await subscriptions_collection().find_one_and_update(
        {
            "user_id": int(user_id),
            "active": True,
            "expiry_date": {"$lte": now},
            "$or": [
                {"expiry_claim_token": {"$exists": False}},
                {"expiry_claim_token": None},
                {"expiry_claimed_at": {"$lt": stale_before}},
            ],
        },
        {
            "$set": {
                "expiring": True,
                "expiry_claim_token": claim_token,
                "expiry_claimed_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if document is None:
        return None

    document["_claim_token"] = claim_token
    return document


async def release_expiry_claim(
    user_id: int,
    claim_token: str,
    error: str = "",
) -> bool:
    now = datetime.now(timezone.utc)
    result = await subscriptions_collection().update_one(
        {
            "user_id": int(user_id),
            "active": True,
            "expiry_claim_token": str(claim_token),
        },
        {
            "$set": {
                "expiring": False,
                "last_expiry_error": str(error)[:500],
                "updated_at": now,
            },
            "$unset": {
                "expiry_claim_token": "",
                "expiry_claimed_at": "",
            },
        },
    )
    return result.modified_count == 1


async def complete_expiry_claim(
    user_id: int,
    claim_token: str,
    expected_expiry,
) -> bool:
    """
    Finalize only the subscription claimed by this worker and only if the
    expiry value did not change while channel access was being revoked.
    """
    now = datetime.now(timezone.utc)
    result = await subscriptions_collection().update_one(
        {
            "user_id": int(user_id),
            "active": True,
            "expiry_date": expected_expiry,
            "expiry_claim_token": str(claim_token),
        },
        {
            "$set": {
                "active": False,
                "expiring": False,
                "expired_at": now,
                "updated_at": now,
                "expiry_notification_sent": False,
            },
            "$unset": {
                "expiry_claim_token": "",
                "expiry_claimed_at": "",
                "last_expiry_error": "",
            },
        },
    )
    return result.modified_count == 1


async def claim_expiry_notification(
    user_id: int,
    *,
    stale_after_seconds: int = 900,
):
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(
        seconds=max(60, int(stale_after_seconds))
    )
    token = uuid4().hex

    document = await subscriptions_collection().find_one_and_update(
        {
            "user_id": int(user_id),
            "active": False,
            "expiry_notification_sent": {"$ne": True},
            "$or": [
                {"expiry_notification_token": {"$exists": False}},
                {"expiry_notification_token": None},
                {"expiry_notification_claimed_at": {"$lt": stale_before}},
            ],
        },
        {
            "$set": {
                "expiry_notification_token": token,
                "expiry_notification_claimed_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if document is None:
        return None

    return token


async def complete_expiry_notification(
    user_id: int,
    token: str,
) -> bool:
    now = datetime.now(timezone.utc)
    result = await subscriptions_collection().update_one(
        {
            "user_id": int(user_id),
            "active": False,
            "expiry_notification_token": str(token),
        },
        {
            "$set": {
                "expiry_notification_sent": True,
                "expiry_notification_sent_at": now,
                "updated_at": now,
            },
            "$unset": {
                "expiry_notification_token": "",
                "expiry_notification_claimed_at": "",
                "last_expiry_notification_error": "",
            },
        },
    )
    return result.modified_count == 1


async def release_expiry_notification(
    user_id: int,
    token: str,
    error: str = "",
) -> bool:
    now = datetime.now(timezone.utc)
    result = await subscriptions_collection().update_one(
        {
            "user_id": int(user_id),
            "active": False,
            "expiry_notification_token": str(token),
        },
        {
            "$set": {
                "last_expiry_notification_error": str(error)[:500],
                "updated_at": now,
            },
            "$unset": {
                "expiry_notification_token": "",
                "expiry_notification_claimed_at": "",
            },
        },
    )
    return result.modified_count == 1


async def get_expired_subscriptions(now=None):
    if now is None:
        now = datetime.now(timezone.utc)

    return await subscriptions_collection().find(
        {
            "active": True,
            "expiry_date": {"$lte": now},
        }
    ).to_list(length=None)


async def get_all_subscriptions():
    return await subscriptions_collection().find().to_list(length=None)


async def is_subscription_active(user_id: int):
    subscription = await get_subscription(user_id)

    if not subscription or not subscription.get("active"):
        return False

    expiry = make_aware(subscription.get("expiry_date"))

    if not expiry:
        return False

    return expiry > datetime.now(timezone.utc)


async def delete_subscription(user_id: int):
    async with subscription_lock(user_id):
        await subscriptions_collection().delete_one({"user_id": user_id})


async def total_subscriptions():
    return await subscriptions_collection().count_documents({})
