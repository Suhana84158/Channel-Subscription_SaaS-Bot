from datetime import datetime, timezone

from database.mongo import get_database

COLLECTION = "sellers"


def sellers_collection():
    return get_database()[COLLECTION]


async def get_seller(owner_id: int):
    return await sellers_collection().find_one({"owner_id": owner_id})


async def create_seller(owner_id: int, first_name=None, username=None):
    now = datetime.now(timezone.utc)

    document = {
        "owner_id": owner_id,
        "first_name": first_name,
        "username": username,
        "active": False,
        "approved": False,
        "suspended": False,
        "plan": None,
        "expiry_date": None,
        "created_at": now,
        "updated_at": now,
    }

    await sellers_collection().insert_one(document)
    return document


async def get_or_create_seller(user):
    seller = await get_seller(user.id)

    if seller:
        return seller

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


async def get_all_sellers():
    return await sellers_collection().find().to_list(length=None)


async def total_sellers():
    return await sellers_collection().count_documents({})
