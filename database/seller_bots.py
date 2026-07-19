from datetime import datetime, timezone, timedelta
from database.mongo import get_database
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from utils.crypto import decrypt_secret, encrypt_secret

COLLECTION = "seller_bots"


class BotOwnershipError(RuntimeError):
    """Raised when a Telegram bot is already owned by another seller."""


def seller_bots_collection():
    return get_database()[COLLECTION]


async def initialize_seller_bot_indexes():
    col = seller_bots_collection()
    # Old builds used a unique owner_id index, which blocked multiple clone bots.
    for index in await col.list_indexes().to_list(length=None):
        key = index.get("key", {})
        if index.get("unique") and list(key.items()) == [("owner_id", 1)]:
            try:
                await col.drop_index(index["name"])
            except Exception:
                pass
    await col.create_index("owner_id")
    await col.create_index("bot_id", unique=True)
    await col.create_index("bot_username_normalized", unique=True)
    await col.create_index("active")
    await col.create_index("runtime_status")
    await col.create_index("next_recovery_at")
    await col.create_index([("owner_id", 1), ("created_at", 1)])

    # Preserve the existing first bot's old owner-scoped data.
    cursor = col.find({"data_owner_id": {"$exists": False}})
    async for record in cursor:
        await col.update_one(
            {"_id": record["_id"]},
            {"$set": {"data_owner_id": int(record["owner_id"])}}
        )


async def get_bot(owner_id: int):
    """Backward-compatible: return seller's first/oldest clone bot."""
    return await seller_bots_collection().find_one(
        {"owner_id": int(owner_id)}, sort=[("created_at", 1)]
    )


async def get_bots(owner_id: int):
    return await seller_bots_collection().find(
        {"owner_id": int(owner_id)}
    ).sort("created_at", 1).to_list(length=None)


async def count_owner_bots(owner_id: int):
    return await seller_bots_collection().count_documents({"owner_id": int(owner_id)})


async def get_bot_by_bot_id(bot_id: int):
    return await seller_bots_collection().find_one({"bot_id": int(bot_id)})


async def get_bot_by_data_owner_id(data_owner_id: int):
    return await seller_bots_collection().find_one({"data_owner_id": int(data_owner_id)})


async def get_bot_by_username(bot_username: str):
    return await seller_bots_collection().find_one({
        "bot_username_normalized": bot_username.lstrip("@").lower()
    })


async def save_bot(owner_id: int, bot_id: int, bot_name: str, bot_username: str, bot_token: str):
    """
    Save a clone bot without allowing ownership takeover.

    The database query itself enforces ownership, so two sellers submitting the
    same token concurrently cannot transfer the bot between accounts.
    """
    owner_id = int(owner_id)
    bot_id = int(bot_id)
    now = datetime.now(timezone.utc)

    existing = await get_bot_by_bot_id(bot_id)
    if existing and int(existing.get("owner_id", 0)) != owner_id:
        raise BotOwnershipError("This bot is already connected to another seller.")

    # First bot keeps old owner_id scope so existing plans/users/settings remain intact.
    first_bot = await get_bot(owner_id)
    data_owner_id = (
        int(existing.get("data_owner_id"))
        if existing and existing.get("data_owner_id") is not None
        else owner_id if not first_bot
        else bot_id
    )

    query = {
        "bot_id": bot_id,
        "$or": [
            {"owner_id": owner_id},
            {"owner_id": {"$exists": False}},
        ],
    }

    try:
        result = await seller_bots_collection().update_one(
            query,
            {
                "$set": {
                    "owner_id": owner_id,
                    "data_owner_id": data_owner_id,
                    "bot_id": bot_id,
                    "bot_name": bot_name,
                    "bot_username": bot_username,
                    "bot_username_normalized": bot_username.lstrip("@").lower(),
                    "bot_token_encrypted": encrypt_secret(bot_token),
                    "active": True,
                    "status": "registered",
                    "runtime_status": "registered",
                    "runtime_error": None,
                    "invalid_token_at": None,
                    "next_recovery_at": None,
                    "consecutive_recovery_failures": 0,
                    "updated_at": now,
                },
                "$unset": {
                    "bot_token": "",
                    "recovery_claimed_at": "",
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except DuplicateKeyError as exc:
        raise BotOwnershipError(
            "This bot is already connected to another seller."
        ) from exc

    if result.matched_count == 0 and result.upserted_id is None:
        raise BotOwnershipError("This bot is already connected to another seller.")

    record = await get_bot_by_bot_id(bot_id)
    if not record or int(record.get("owner_id", 0)) != owner_id:
        raise BotOwnershipError("Unable to verify clone bot ownership.")

    return record


async def get_decrypted_bot_token(bot_id: int):
    record = await get_bot_by_bot_id(bot_id)
    if not record:
        record = await get_bot(bot_id)
    if not record:
        return None
    encrypted = record.get("bot_token_encrypted")
    return decrypt_secret(encrypted) if encrypted else None


async def get_all_active_bots():
    return await seller_bots_collection().find({
        "active": True,
        "bot_token_encrypted": {"$exists": True, "$ne": None},
        "runtime_status": {"$nin": ["invalid_token", "token_missing"]},
    }).to_list(length=None)


async def mark_invalid_token(bot_id: int, error=None):
    """Disable automatic retries until the seller submits a valid token."""
    now = datetime.now(timezone.utc)
    await seller_bots_collection().update_one(
        {"bot_id": int(bot_id)},
        {
            "$set": {
                "active": False,
                "status": "invalid_token",
                "runtime_status": "invalid_token",
                "runtime_error": str(error or "Telegram rejected the bot token")[:500],
                "invalid_token_at": now,
                "next_recovery_at": None,
                "updated_at": now,
            },
            "$unset": {"recovery_claimed_at": ""},
        },
    )


async def count_invalid_token_bots(owner_id: int | None = None):
    query = {"runtime_status": "invalid_token"}
    if owner_id is not None:
        query["owner_id"] = int(owner_id)
    return await seller_bots_collection().count_documents(query)


async def set_bot_active(bot_id: int, active: bool):
    record = await get_bot_by_bot_id(bot_id) or await get_bot(bot_id)
    query = {"bot_id": int(record["bot_id"])} if record else {"bot_id": int(bot_id)}
    await seller_bots_collection().update_one(
        query,
        {"$set": {
            "active": bool(active),
            "status": "active" if active else "paused",
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def set_runtime_status(bot_id: int, status: str, error=None):
    record = await get_bot_by_bot_id(bot_id) or await get_bot(bot_id)
    query = {"bot_id": int(record["bot_id"])} if record else {"bot_id": int(bot_id)}
    await seller_bots_collection().update_one(
        query,
        {"$set": {
            "runtime_status": status,
            "runtime_error": error,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def claim_runtime_recovery(bot_id: int, cooldown_seconds: int = 300):
    """Atomically claim one recovery attempt and prevent restart storms."""
    bot_id = int(bot_id)
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(30, int(cooldown_seconds)))
    return await seller_bots_collection().find_one_and_update(
        {
            "bot_id": bot_id,
            "active": True,
            "runtime_status": {"$nin": ["invalid_token", "token_missing", "plan_limit_paused"]},
            "$or": [
                {"recovery_claimed_at": {"$exists": False}},
                {"recovery_claimed_at": None},
                {"recovery_claimed_at": {"$lte": stale_before}},
            ],
        },
        {
            "$set": {
                "recovery_claimed_at": now,
                "runtime_status": "recovering",
                "updated_at": now,
            },
            "$inc": {"recovery_claim_count": 1},
        },
        return_document=ReturnDocument.AFTER,
    )


async def finish_runtime_recovery(bot_id: int, success: bool, error=None, retry_after_seconds: int = 300):
    """Persist recovery result and release the active recovery claim."""
    now = datetime.now(timezone.utc)
    update = {
        "$set": {
            "runtime_status": "running" if success else "recovery_failed",
            "runtime_error": None if success else str(error or "Recovery failed")[:500],
            "last_recovery_at": now,
            "next_recovery_at": None if success else now + timedelta(seconds=max(60, int(retry_after_seconds))),
            "updated_at": now,
        },
        "$unset": {"recovery_claimed_at": ""},
    }
    if success:
        update["$set"]["consecutive_recovery_failures"] = 0
    else:
        update["$inc"] = {"consecutive_recovery_failures": 1}
    await seller_bots_collection().update_one({"bot_id": int(bot_id)}, update)


async def recovery_allowed(record: dict, now=None) -> bool:
    """Return False while a failed bot is inside its persisted cooldown."""
    now = now or datetime.now(timezone.utc)
    next_at = record.get("next_recovery_at") if record else None
    if next_at is None:
        return True
    if getattr(next_at, "tzinfo", None) is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    return next_at <= now


async def delete_bot(owner_id: int, bot_id: int | None = None):
    query = {"owner_id": int(owner_id)}
    if bot_id is not None:
        query["bot_id"] = int(bot_id)
        return await seller_bots_collection().delete_one(query)
    return await seller_bots_collection().delete_many(query)


async def bot_exists(owner_id: int):
    return await count_owner_bots(owner_id) > 0


async def total_bots():
    return await seller_bots_collection().count_documents({})
