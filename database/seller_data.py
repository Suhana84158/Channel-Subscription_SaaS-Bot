from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from database.mongo import get_database

SELLER_SETTINGS = "seller_settings"
SELLER_PLANS = "seller_plans"
SELLER_CHANNELS = "seller_channels"
SELLER_USERS = "seller_users"
SELLER_PAYMENTS = "seller_payments"
SELLER_SUBSCRIPTIONS = "seller_subscriptions"
SELLER_REFERRALS = "seller_referrals"
SELLER_COUPONS = "seller_coupons"


def _collection(name: str):
    return get_database()[name]


async def initialize_seller_data_indexes() -> None:
    await _collection(SELLER_SETTINGS).create_index("owner_id", unique=True)
    await _collection(SELLER_PLANS).create_index([("owner_id", 1), ("plan_id", 1)], unique=True)
    await _collection(SELLER_PLANS).create_index([("owner_id", 1), ("active", 1)])
    await _collection(SELLER_CHANNELS).create_index([("owner_id", 1), ("chat_id", 1)], unique=True)
    await _collection(SELLER_CHANNELS).create_index([("owner_id", 1), ("active", 1)])
    await _collection(SELLER_USERS).create_index([("owner_id", 1), ("user_id", 1)], unique=True)
    await _collection(SELLER_PAYMENTS).create_index([("owner_id", 1), ("status", 1), ("created_at", -1)])
    await _collection(SELLER_PAYMENTS).create_index([("owner_id", 1), ("user_id", 1), ("created_at", -1)])
    await _collection(SELLER_SUBSCRIPTIONS).create_index([("owner_id", 1), ("user_id", 1)], unique=True)
    await _collection(SELLER_SUBSCRIPTIONS).create_index([("owner_id", 1), ("active", 1), ("expiry_date", 1)])
    await _collection(SELLER_REFERRALS).create_index([("owner_id", 1), ("user_id", 1)], unique=True)
    await _collection(SELLER_COUPONS).create_index([("owner_id", 1), ("code_normalized", 1)], unique=True)


async def ensure_seller_defaults(owner_id: int, bot_name: str = "Subscription Bot") -> dict:
    now = datetime.now(timezone.utc)
    defaults = {
        "owner_id": owner_id,
        "bot_name": bot_name,
        "welcome_message": f"👋 Welcome to {bot_name}!",
        "support_username": "",
        "currency": "INR",
        "timezone": "Asia/Kolkata",
        "reminder_days": 1,
        "created_at": now,
        "updated_at": now,
    }
    await _collection(SELLER_SETTINGS).update_one(
        {"owner_id": owner_id},
        {"$setOnInsert": defaults},
        upsert=True,
    )
    return await get_seller_settings(owner_id)


async def get_seller_settings(owner_id: int) -> dict:
    return await _collection(SELLER_SETTINGS).find_one({"owner_id": owner_id}) or {}


async def set_seller_setting(owner_id: int, key: str, value: Any) -> None:
    allowed = {
        "bot_name", "welcome_message", "support_username", "currency",
        "timezone", "reminder_days", "upi_id", "upi_name", "upi_qr_file_id",
    }
    if key not in allowed:
        raise ValueError(f"Unsupported seller setting: {key}")
    now = datetime.now(timezone.utc)
    await _collection(SELLER_SETTINGS).update_one(
        {"owner_id": owner_id},
        {
            "$set": {key: value, "updated_at": now},
            "$setOnInsert": {"owner_id": owner_id, "created_at": now},
        },
        upsert=True,
    )


async def create_seller_plan(owner_id: int, name: str, duration_text: str, duration_minutes: int, price: float) -> dict:
    now = datetime.now(timezone.utc)
    plan = {
        "owner_id": owner_id,
        "plan_id": uuid4().hex[:12],
        "name": name.strip(),
        "duration_text": duration_text.strip().lower(),
        "duration_minutes": int(duration_minutes),
        "price": float(price),
        "active": True,
        "created_at": now,
        "updated_at": now,
    }
    await _collection(SELLER_PLANS).insert_one(plan)
    return plan


async def get_seller_plan(owner_id: int, plan_id: str) -> dict | None:
    return await _collection(SELLER_PLANS).find_one({"owner_id": owner_id, "plan_id": plan_id})


async def get_all_seller_plans(owner_id: int) -> list[dict]:
    return await _collection(SELLER_PLANS).find({"owner_id": owner_id}).sort("created_at", -1).to_list(length=100)


async def get_active_seller_plans(owner_id: int) -> list[dict]:
    return await _collection(SELLER_PLANS).find({"owner_id": owner_id, "active": True}).sort("price", 1).to_list(length=100)


async def update_seller_plan(owner_id: int, plan_id: str, name: str, duration_text: str, duration_minutes: int, price: float) -> bool:
    result = await _collection(SELLER_PLANS).update_one(
        {"owner_id": owner_id, "plan_id": plan_id},
        {"$set": {
            "name": name.strip(),
            "duration_text": duration_text.strip().lower(),
            "duration_minutes": int(duration_minutes),
            "price": float(price),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return result.matched_count > 0


async def set_seller_plan_active(owner_id: int, plan_id: str, active: bool) -> bool:
    result = await _collection(SELLER_PLANS).update_one(
        {"owner_id": owner_id, "plan_id": plan_id},
        {"$set": {"active": bool(active), "updated_at": datetime.now(timezone.utc)}},
    )
    return result.matched_count > 0


async def delete_seller_plan(owner_id: int, plan_id: str) -> bool:
    result = await _collection(SELLER_PLANS).delete_one({"owner_id": owner_id, "plan_id": plan_id})
    return result.deleted_count > 0


async def count_seller_channels(owner_id: int) -> int:
    return await _collection(SELLER_CHANNELS).count_documents({"owner_id": owner_id, "active": True})


async def count_seller_users(owner_id: int) -> int:
    return await _collection(SELLER_USERS).count_documents({"owner_id": owner_id})
