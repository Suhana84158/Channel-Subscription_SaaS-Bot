import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

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
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    return expiry


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
