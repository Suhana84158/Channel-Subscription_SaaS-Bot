from datetime import datetime, timezone
from database.mongo import get_database

COLLECTION = "seller_staff"

DEFAULT_PERMISSIONS = {
    "admin": ["users", "payments", "plans", "channels", "broadcast", "statistics", "support", "guard"],
    "moderator": ["users", "payments", "support", "guard_logs"],
}


def _col():
    return get_database()[COLLECTION]


async def ensure_staff_indexes():
    await _col().create_index([("owner_id", 1), ("user_id", 1)], unique=True)
    await _col().create_index([("owner_id", 1), ("status", 1), ("role", 1)])


async def promote_staff(owner_id: int, user_id: int, role: str, promoted_by: int, username: str = "", full_name: str = ""):
    role = role.lower().strip()
    if role not in DEFAULT_PERMISSIONS:
        raise ValueError("Role must be admin or moderator")
    now = datetime.now(timezone.utc)
    await _col().update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)},
        {"$set": {
            "role": role,
            "status": "active",
            "permissions": DEFAULT_PERMISSIONS[role],
            "username": (username or "").lstrip("@"),
            "full_name": full_name or "",
            "promoted_by": int(promoted_by),
            "promoted_at": now,
            "updated_at": now,
        }, "$setOnInsert": {"created_at": now, "total_actions": 0}},
        upsert=True,
    )
    return await get_staff(owner_id, user_id)


async def get_staff(owner_id: int, user_id: int):
    return await _col().find_one({"owner_id": int(owner_id), "user_id": int(user_id)})


async def active_staff(owner_id: int, user_id: int):
    return await _col().find_one({"owner_id": int(owner_id), "user_id": int(user_id), "status": "active"})


async def list_staff(owner_id: int):
    return await _col().find({"owner_id": int(owner_id)}).sort([("role", 1), ("promoted_at", -1)]).to_list(length=200)


async def set_staff_status(owner_id: int, user_id: int, status: str):
    await _col().update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)},
        {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
    )


async def remove_staff(owner_id: int, user_id: int):
    return await _col().delete_one({"owner_id": int(owner_id), "user_id": int(user_id)})


async def log_staff_action(owner_id: int, user_id: int, action: str):
    await _col().update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)},
        {"$set": {"last_action": action, "last_action_at": datetime.now(timezone.utc)}, "$inc": {"total_actions": 1}},
    )
