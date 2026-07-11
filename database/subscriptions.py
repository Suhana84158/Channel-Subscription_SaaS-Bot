from datetime import datetime, timedelta, timezone

from database.mongo import get_database

COLLECTION = "subscriptions"


def subscriptions_collection():
    return get_database()[COLLECTION]


def make_aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
    subscription = await get_subscription(user_id)
    now = datetime.now(timezone.utc)

    expiry_date = None
    if subscription:
        expiry_date = make_aware(subscription.get("expiry_date"))

    if duration_minutes > 0:
        add_time = timedelta(minutes=duration_minutes)
    else:
        add_time = timedelta(days=duration_days)

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
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    return expiry


async def expire_subscription(user_id: int):
    await subscriptions_collection().update_one(
        {"user_id": user_id},
        {
            "$set": {
                "active": False,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


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
    await subscriptions_collection().delete_one({"user_id": user_id})


async def total_subscriptions():
    return await subscriptions_collection().count_documents({})
