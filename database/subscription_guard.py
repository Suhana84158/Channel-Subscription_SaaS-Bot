from datetime import datetime, timezone
from typing import Any

from database.mongo import get_database

INVITES = "subscription_guard_invites"
WHITELIST = "subscription_guard_whitelist"
LOGS = "subscription_guard_logs"
SETTINGS = "subscription_guard_settings"
ATTEMPTS = "subscription_guard_attempts"

DEFAULT_SETTINGS = {
    "enabled": True,
    "unauthorized_join_protection": True,
    "auto_remove_expired": True,
    "auto_revoke_invites": True,
    "whitelist_admin_added": True,
    "log_events": True,
    "notify_seller": True,
}


def _c(name: str):
    return get_database()[name]


async def get_guard_settings(owner_id: int) -> dict[str, Any]:
    owner_id = int(owner_id)
    doc = await _c(SETTINGS).find_one({"owner_id": owner_id}) or {}
    result = dict(DEFAULT_SETTINGS)
    result.update({key: doc.get(key, value) for key, value in DEFAULT_SETTINGS.items()})
    result["owner_id"] = owner_id
    return result


async def set_guard_setting(owner_id: int, key: str, value: bool) -> dict[str, Any]:
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Unknown guard setting: {key}")
    now = datetime.now(timezone.utc)
    await _c(SETTINGS).update_one(
        {"owner_id": int(owner_id)},
        {"$set": {key: bool(value), "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return await get_guard_settings(owner_id)


async def reset_guard_settings(owner_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    await _c(SETTINGS).update_one(
        {"owner_id": int(owner_id)},
        {"$set": {**DEFAULT_SETTINGS, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return await get_guard_settings(owner_id)


async def save_invite(owner_id: int, user_id: int, chat_id: int, invite_link: str):
    now = datetime.now(timezone.utc)
    await _c(INVITES).update_one(
        {"owner_id": int(owner_id), "invite_link": invite_link},
        {
            "$set": {
                "owner_id": int(owner_id), "user_id": int(user_id), "chat_id": int(chat_id),
                "invite_link": invite_link, "active": True, "used": False, "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    await deactivate_other_invites(
        owner_id,
        user_id,
        chat_id,
        invite_link,
    )


async def get_active_invite(owner_id: int, user_id: int, chat_id: int):
    """Return the latest unused active invite for one user and chat."""
    return await _c(INVITES).find_one(
        {
            "owner_id": int(owner_id),
            "user_id": int(user_id),
            "chat_id": int(chat_id),
            "active": True,
            "used": False,
        },
        sort=[("updated_at", -1)],
    )


async def deactivate_other_invites(
    owner_id: int,
    user_id: int,
    chat_id: int,
    keep_invite_link: str,
):
    """Keep one current invite active and retire older duplicates."""
    now = datetime.now(timezone.utc)
    await _c(INVITES).update_many(
        {
            "owner_id": int(owner_id),
            "user_id": int(user_id),
            "chat_id": int(chat_id),
            "invite_link": {"$ne": keep_invite_link},
            "active": True,
        },
        {
            "$set": {
                "active": False,
                "replaced_at": now,
                "updated_at": now,
            }
        },
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
                "owner_id": int(owner_id), "chat_id": int(chat_id), "user_id": int(user_id),
                "added_by": int(added_by) if added_by else None, "active": True, "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def is_whitelisted(owner_id: int, chat_id: int, user_id: int) -> bool:
    return bool(await _c(WHITELIST).find_one({
        "owner_id": int(owner_id), "chat_id": int(chat_id), "user_id": int(user_id), "active": True
    }))


async def log_guard_event(owner_id: int, chat_id: int, user_id: int, action: str, reason: str = "", **extra):
    settings = await get_guard_settings(owner_id)
    if not settings.get("log_events", True):
        return
    await _c(LOGS).insert_one({
        "owner_id": int(owner_id), "chat_id": int(chat_id), "user_id": int(user_id),
        "action": action, "reason": reason, "created_at": datetime.now(timezone.utc), **extra,
    })


async def record_join_attempt(owner_id: int, chat_id: int, user_id: int) -> int:
    now = datetime.now(timezone.utc)
    result = await _c(ATTEMPTS).find_one_and_update(
        {"owner_id": int(owner_id), "chat_id": int(chat_id), "user_id": int(user_id)},
        {
            "$inc": {"attempts": 1},
            "$set": {"last_attempt_at": now},
            "$setOnInsert": {"first_attempt_at": now},
        },
        upsert=True,
        return_document=True,
    )
    return int((result or {}).get("attempts", 1))


async def recent_guard_logs(owner_id: int, limit: int = 10):
    return await _c(LOGS).find({"owner_id": int(owner_id)}).sort("created_at", -1).limit(limit).to_list(length=limit)


async def guard_statistics(owner_id: int) -> dict[str, int]:
    owner_id = int(owner_id)
    pipeline = [
        {"$match": {"owner_id": owner_id}},
        {"$group": {"_id": "$action", "count": {"$sum": 1}}},
    ]
    rows = await _c(LOGS).aggregate(pipeline).to_list(length=100)
    stats = {str(row["_id"]): int(row["count"]) for row in rows}
    stats["join_attempts"] = int(sum([
        int(row.get("attempts", 0))
        async for row in _c(ATTEMPTS).find({"owner_id": owner_id}, {"attempts": 1})
    ]))
    return stats


async def clear_guard_logs(owner_id: int):
    await _c(LOGS).delete_many({"owner_id": int(owner_id)})
    await _c(ATTEMPTS).delete_many({"owner_id": int(owner_id)})
