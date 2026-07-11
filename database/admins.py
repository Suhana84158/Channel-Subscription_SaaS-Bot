from database.mongo import get_database
from config import ADMIN_IDS

COLLECTION = "admins"


def admins_collection():
    return get_database()[COLLECTION]


async def initialize_admins():
    """
    Add default admins from config if they don't exist.
    """
    for admin_id in ADMIN_IDS:
        exists = await admins_collection().find_one(
            {"admin_id": admin_id}
        )

        if not exists:
            await admins_collection().insert_one(
                {
                    "admin_id": admin_id,
                    "role": "super_admin",
                    "active": True,
                }
            )


async def is_admin(user_id: int) -> bool:
    admin = await admins_collection().find_one(
        {
            "admin_id": user_id,
            "active": True,
        }
    )

    return admin is not None


async def get_all_admins():
    return await admins_collection().find(
        {"active": True}
    ).to_list(length=None)


async def add_admin(user_id: int, role: str = "admin"):
    await admins_collection().update_one(
        {"admin_id": user_id},
        {
            "$set": {
                "role": role,
                "active": True,
            }
        },
        upsert=True,
    )


async def remove_admin(user_id: int):
    await admins_collection().update_one(
        {"admin_id": user_id},
        {
            "$set": {
                "active": False
            }
        }
    )
