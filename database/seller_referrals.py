from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from database.mongo import get_database
from database.seller_subscriptions import (
    assign_plan_with_history,
    get_assignment,
    get_config,
    update_config,
)

COLLECTION = "seller_referrals"


def collection():
    return get_database()[COLLECTION]


async def initialize_seller_referral_indexes():
    await collection().create_index("referred_seller_id", unique=True)
    await collection().create_index([("referrer_seller_id", 1), ("created_at", -1)])


async def register_seller_referral(referrer_seller_id: int, referred_seller_id: int):
    referrer_seller_id = int(referrer_seller_id)
    referred_seller_id = int(referred_seller_id)
    if referrer_seller_id == referred_seller_id:
        return None

    now = datetime.now(timezone.utc)
    return await collection().find_one_and_update(
        {"referred_seller_id": referred_seller_id},
        {
            "$setOnInsert": {
                "referrer_seller_id": referrer_seller_id,
                "referred_seller_id": referred_seller_id,
                "status": "registered",
                "rewarded": False,
                "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def seller_referral_stats(referrer_seller_id: int):
    query = {"referrer_seller_id": int(referrer_seller_id)}
    total = await collection().count_documents(query)
    rewarded = await collection().count_documents({**query, "rewarded": True})
    return {"total": total, "rewarded": rewarded}


async def reward_seller_referral(referred_seller_id: int, approved_by: int | None = None):
    now = datetime.now(timezone.utc)
    referral = await collection().find_one_and_update(
        {"referred_seller_id": int(referred_seller_id), "rewarded": False},
        {"$set": {"rewarded": True, "status": "rewarded", "rewarded_at": now, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not referral:
        return None

    cfg = await get_config()
    reward_days = max(0, int(cfg.get("seller_referral_reward_days", 7) or 0))
    reward_plan_id = str(cfg.get("seller_referral_reward_plan_id", "starter") or "starter")
    referrer_id = int(referral["referrer_seller_id"])

    if reward_days <= 0:
        return {**referral, "reward_days": 0, "reward_plan_id": reward_plan_id}

    assignment = await get_assignment(referrer_id)
    active_plan_id = None
    current_expiry = None
    if assignment:
        active_plan_id = assignment.get("plan_id")
        current_expiry = assignment.get("expiry_date")
        if current_expiry and current_expiry.tzinfo is None:
            current_expiry = current_expiry.replace(tzinfo=timezone.utc)

    # Keep an active paid plan and extend from its current expiry.
    if active_plan_id and active_plan_id != "free" and current_expiry and current_expiry > now:
        new_expiry = current_expiry + timedelta(days=reward_days)
        await get_database()["seller_plan_assignments"].update_one(
            {"owner_id": referrer_id},
            {"$set": {"expiry_date": new_expiry, "source": "seller_referral", "updated_at": now}},
        )
        from database.seller_subscriptions import record_history
        await record_history(
            referrer_id,
            "referral_reward",
            previous_plan=active_plan_id,
            new_plan=active_plan_id,
            days=reward_days,
            source="seller_referral",
            amount=0,
            approved_by=approved_by,
            expiry_date=new_expiry,
            referred_seller_id=int(referred_seller_id),
        )
    else:
        await assign_plan_with_history(
            referrer_id,
            reward_plan_id,
            reward_days,
            source="seller_referral",
            amount=0,
            approved_by=approved_by,
        )

    return {**referral, "reward_days": reward_days, "reward_plan_id": reward_plan_id}


async def set_seller_referral_settings(days: int, plan_id: str):
    return await update_config(
        seller_referral_reward_days=max(0, int(days)),
        seller_referral_reward_plan_id=str(plan_id).strip().lower(),
    )
