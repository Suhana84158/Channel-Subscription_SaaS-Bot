from datetime import datetime, timedelta, timezone

from database.mongo import get_database

COLLECTION = "sellers"
DEFAULT_TRIAL_DAYS = 7


def sellers_collection():
    return get_database()[COLLECTION]


async def initialize_seller_indexes():
    await sellers_collection().create_index("owner_id", unique=True)
    await sellers_collection().create_index([("active", 1), ("suspended", 1)])
    await sellers_collection().create_index("trial_expiry")


async def get_seller(owner_id: int):
    return await sellers_collection().find_one({"owner_id": owner_id})


async def create_seller(owner_id: int, first_name=None, username=None):
    now = datetime.now(timezone.utc)
    document = {
        "owner_id": owner_id,
        "first_name": first_name,
        "username": username,
        "active": True,
        "approved": True,
        "suspended": False,
        "plan": "trial",
        "trial_expiry": now + timedelta(days=DEFAULT_TRIAL_DAYS),
        "expiry_date": now + timedelta(days=DEFAULT_TRIAL_DAYS),
        "created_at": now,
        "updated_at": now,
    }

    await sellers_collection().update_one(
        {"owner_id": owner_id},
        {"$setOnInsert": document},
        upsert=True,
    )
    return await get_seller(owner_id)


async def get_or_create_seller(user):
    seller = await get_seller(user.id)
    if seller:
        await sellers_collection().update_one(
            {"owner_id": user.id},
            {
                "$set": {
                    "first_name": user.first_name,
                    "username": user.username,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return await get_seller(user.id)

    return await create_seller(
        owner_id=user.id,
        first_name=user.first_name,
        username=user.username,
    )


async def approve_seller(owner_id: int):
    await sellers_collection().update_one(
        {"owner_id": owner_id},
        {
            "$set": {
                "approved": True,
                "active": True,
                "suspended": False,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


async def suspend_seller(owner_id: int):
    await sellers_collection().update_one(
        {"owner_id": owner_id},
        {
            "$set": {
                "suspended": True,
                "active": False,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


async def unsuspend_seller(owner_id: int):
    await sellers_collection().update_one(
        {"owner_id": owner_id},
        {
            "$set": {
                "suspended": False,
                "active": True,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


async def get_all_sellers(limit: int = 100):
    return await sellers_collection().find().sort("created_at", -1).to_list(length=limit)


async def total_sellers():
    return await sellers_collection().count_documents({})
