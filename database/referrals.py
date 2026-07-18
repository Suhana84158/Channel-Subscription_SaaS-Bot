from datetime import datetime, timezone

from pymongo import ReturnDocument

from database.mongo import get_database

COLLECTION = "referrals"


def referrals_collection():
    return get_database()[COLLECTION]


async def create_referral(referrer_id: int, referred_id: int):
    referrer_id = int(referrer_id)
    referred_id = int(referred_id)
    if referrer_id == referred_id:
        return None

    now = datetime.now(timezone.utc)
    return await referrals_collection().find_one_and_update(
        {"referred_id": referred_id},
        {
            "$setOnInsert": {
                "referrer_id": referrer_id,
                "referred_id": referred_id,
                "bonus_given": False,
                "reward_status": "pending",
                "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def referral_exists(referred_id: int):
    return await referrals_collection().find_one({"referred_id": int(referred_id)})


async def get_referrals(referrer_id: int):
    return await referrals_collection().find(
        {"referrer_id": int(referrer_id)}
    ).to_list(length=None)


async def total_referrals(referrer_id: int):
    return await referrals_collection().count_documents(
        {"referrer_id": int(referrer_id)}
    )


async def claim_referral_reward(referred_id: int, payment_id: str):
    now = datetime.now(timezone.utc)
    return await referrals_collection().find_one_and_update(
        {
            "referred_id": int(referred_id),
            "bonus_given": {"$ne": True},
            "reward_status": {"$in": [None, "pending", "failed"]},
        },
        {
            "$set": {
                "reward_status": "processing",
                "reward_payment_id": str(payment_id),
                "reward_claimed_at": now,
                "updated_at": now,
            },
            "$inc": {"reward_attempts": 1},
        },
        return_document=ReturnDocument.AFTER,
    )


async def complete_referral_reward(referred_id: int, payment_id: str, reward_days: int):
    now = datetime.now(timezone.utc)
    result = await referrals_collection().update_one(
        {
            "referred_id": int(referred_id),
            "reward_status": "processing",
            "reward_payment_id": str(payment_id),
        },
        {
            "$set": {
                "bonus_given": True,
                "reward_status": "completed",
                "reward_days": int(reward_days),
                "rewarded_at": now,
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


async def fail_referral_reward(referred_id: int, payment_id: str, error: str):
    now = datetime.now(timezone.utc)
    await referrals_collection().update_one(
        {
            "referred_id": int(referred_id),
            "reward_status": "processing",
            "reward_payment_id": str(payment_id),
        },
        {
            "$set": {
                "reward_status": "failed",
                "reward_error": str(error)[:500],
                "updated_at": now,
            }
        },
    )


async def mark_bonus_given(referred_id: int):
    now = datetime.now(timezone.utc)
    await referrals_collection().update_one(
        {"referred_id": int(referred_id)},
        {
            "$set": {
                "bonus_given": True,
                "reward_status": "completed",
                "rewarded_at": now,
                "updated_at": now,
            }
        },
    )
