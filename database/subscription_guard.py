from datetime import datetime, timezone
from database.mongo import get_database

INVITES = "subscription_guard_invites"
WHITELIST = "subscription_guard_whitelist"
LOGS = "subscription_guard_logs"


def _c(name):
    return get_database()[name]


async def save_invite(owner_id: int, user_id: int, chat_id: int, invite_link: str):
    now = datetime.now(timezone.utc)
    await _c(INVITES).update_one(
        {"owner_id": int(owner_id), "invite_link": invite_link},
        {
            "$set": {
                "owner_id": int(owner_id),
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "invite_link": invite_link,
                "active": True,
                "used": False,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def mark_invite_used(owner_id: int, invite_link: str):
    await _c(INVITES).update_one(
        {"owner_id": int(owner_id), "invite_link": invite_link},
        {"$set": {"used": True, "active": False, "used_at": datetime.now(timezone.utc)}},
    )


async def active_invites_for_user(owner_id: int, user_id: int):
    return await _c(INVITES).find(
        {"owner_id": int(owner_id), "user_id": int(user_id), "active": True}
    ).to_list(length=500)


async def deactivate_invite(owner_id: int, invite_link: str):
    await _c(INVITES).update_one(
        {"owner_id": int(owner_id), "invite_link": invite_link},
        {"$set": {"active": False, "revoked_at": datetime.now(timezone.utc)}},
    )


async def add_whitelist(owner_id: int, chat_id: int, user_id: int, added_by: int | None = None):
    now = datetime.now(timezone.utc)
    await _c(WHITELIST).update_one(
        {"owner_id": int(owner_id), "chat_id": int(chat_id), "user_id": int(user_id)},
        {
            "$set": {
                "owner_id": int(owner_id),
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "added_by": int(added_by) if added_by else None,
                "active": True,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def is_whitelisted(owner_id: int, chat_id: int, user_id: int) -> bool:
    return bool(await _c(WHITELIST).find_one({
        "owner_id": int(owner_id), "chat_id": int(chat_id), "user_id": int(user_id), "active": True
    }))


async def log_guard_event(owner_id: int, chat_id: int, user_id: int, action: str, reason: str = ""):
    await _c(LOGS).insert_one({
        "owner_id": int(owner_id),
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "action": action,
        "reason": reason,
        "created_at": datetime.now(timezone.utc),
    })
