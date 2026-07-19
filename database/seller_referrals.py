from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database.mongo import get_database
from database.seller_subscriptions import get_assignment, get_config, record_history, update_config

COLLECTION = "seller_referrals"
ASSIGNMENTS = "seller_plan_assignments"
CLAIM_TTL_SECONDS = 300


def collection():
    return get_database()[COLLECTION]


def assignments_collection():
    return get_database()[ASSIGNMENTS]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def initialize_seller_referral_indexes():
    await collection().create_index("referred_seller_id", unique=True)
    await collection().create_index(
        [("referrer_seller_id", 1), ("created_at", -1)]
    )
    await collection().create_index([("status", 1), ("claim_expires_at", 1)])


async def register_seller_referral(referrer_seller_id: int, referred_seller_id: int):
    referrer_seller_id = int(referrer_seller_id)
    referred_seller_id = int(referred_seller_id)
    if referrer_seller_id == referred_seller_id:
        return None

    now = _utcnow()
    reward_key = f"seller_referral:{referred_seller_id}"
    return await collection().find_one_and_update(
        {"referred_seller_id": referred_seller_id},
        {
            "$setOnInsert": {
                "referrer_seller_id": referrer_seller_id,
                "referred_seller_id": referred_seller_id,
                "reward_key": reward_key,
                "status": "registered",
                "rewarded": False,
                "attempts": 0,
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
    processing = await collection().count_documents({**query, "status": "processing"})
    failed = await collection().count_documents({**query, "status": "failed"})
    return {
        "total": total,
        "rewarded": rewarded,
        "processing": processing,
        "failed": failed,
    }


async def _claim_reward(referred_seller_id: int):
    now = _utcnow()
    token = uuid4().hex
    stale_before = now - timedelta(seconds=CLAIM_TTL_SECONDS)
    claimed = await collection().find_one_and_update(
        {
            "referred_seller_id": int(referred_seller_id),
            "rewarded": {"$ne": True},
            "$or": [
                {"status": {"$in": ["registered", "failed"]}},
                {"status": "processing", "claimed_at": {"$lte": stale_before}},
                {"status": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "status": "processing",
                "claim_token": token,
                "claimed_at": now,
                "claim_expires_at": now + timedelta(seconds=CLAIM_TTL_SECONDS),
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
            "$unset": {"last_error": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    return claimed, token


async def _apply_reward_once(
    *,
    referrer_id: int,
    reward_key: str,
    reward_plan_id: str,
    reward_days: int,
):
    """Apply a referral reward once, even after a crash and retry."""
    now = _utcnow()
    assignment = await get_assignment(referrer_id)

    if assignment and reward_key in (assignment.get("applied_referral_rewards") or []):
        return assignment, False

    current_plan = (assignment or {}).get("plan_id")
    current_expiry = _aware((assignment or {}).get("expiry_date"))
    active_paid = bool(
        assignment
        and current_plan
        and current_plan != "free"
        and current_expiry
        and current_expiry > now
    )

    marker_filter = {
        "owner_id": int(referrer_id),
        "applied_referral_rewards": {"$ne": reward_key},
    }

    if active_paid:
        result = await assignments_collection().find_one_and_update(
            marker_filter,
            [
                {
                    "$set": {
                        "expiry_date": {
                            "$dateAdd": {
                                "startDate": "$expiry_date",
                                "unit": "day",
                                "amount": int(reward_days),
                            }
                        },
                        "source": "seller_referral",
                        "updated_at": now,
                        "applied_referral_rewards": {
                            "$setUnion": [
                                {"$ifNull": ["$applied_referral_rewards", []]},
                                [reward_key],
                            ]
                        },
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        if result:
            return result, True
        current = await get_assignment(referrer_id)
        return current, False

    expiry = now + timedelta(days=int(reward_days))
    update = {
        "$set": {
            "plan_id": reward_plan_id,
            "expiry_date": expiry,
            "source": "seller_referral",
            "updated_at": now,
        },
        "$setOnInsert": {
            "owner_id": int(referrer_id),
            "created_at": now,
        },
        "$addToSet": {"applied_referral_rewards": reward_key},
    }

    if assignment:
        result = await assignments_collection().find_one_and_update(
            marker_filter,
            update,
            return_document=ReturnDocument.AFTER,
        )
        if result:
            return result, True
        current = await get_assignment(referrer_id)
        return current, False

    try:
        result = await assignments_collection().find_one_and_update(
            {"owner_id": int(referrer_id)},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return result, True
    except DuplicateKeyError:
        result = await assignments_collection().find_one_and_update(
            marker_filter,
            update,
            return_document=ReturnDocument.AFTER,
        )
        if result:
            return result, True
        current = await get_assignment(referrer_id)
        return current, False


async def reward_seller_referral(
    referred_seller_id: int,
    approved_by: int | None = None,
):
    referral, claim_token = await _claim_reward(referred_seller_id)
    if not referral:
        existing = await collection().find_one(
            {"referred_seller_id": int(referred_seller_id)}
        )
        return existing if existing and existing.get("rewarded") else None

    cfg = await get_config()
    reward_days = max(0, int(cfg.get("seller_referral_reward_days", 7) or 0))
    reward_plan_id = str(
        cfg.get("seller_referral_reward_plan_id", "starter") or "starter"
    ).strip().lower()
    referrer_id = int(referral["referrer_seller_id"])
    reward_key = str(
        referral.get("reward_key") or f"seller_referral:{int(referred_seller_id)}"
    )

    try:
        assignment = None
        applied_now = False
        if reward_days > 0:
            assignment, applied_now = await _apply_reward_once(
                referrer_id=referrer_id,
                reward_key=reward_key,
                reward_plan_id=reward_plan_id,
                reward_days=reward_days,
            )

            if applied_now:
                try:
                    await record_history(
                        referrer_id,
                        "referral_reward",
                        previous_plan=(assignment or {}).get("plan_id"),
                        new_plan=(assignment or {}).get("plan_id") or reward_plan_id,
                        days=reward_days,
                        source="seller_referral",
                        amount=0,
                        approved_by=approved_by,
                        expiry_date=(assignment or {}).get("expiry_date"),
                        referred_seller_id=int(referred_seller_id),
                        reward_key=reward_key,
                    )
                except Exception:
                    # The reward itself is already protected by the assignment marker.
                    pass

        now = _utcnow()
        completed = await collection().find_one_and_update(
            {
                "_id": referral["_id"],
                "claim_token": claim_token,
                "status": "processing",
            },
            {
                "$set": {
                    "rewarded": True,
                    "status": "rewarded",
                    "rewarded_at": now,
                    "updated_at": now,
                    "reward_days": reward_days,
                    "reward_plan_id": reward_plan_id,
                    "reward_key": reward_key,
                    "approved_by": approved_by,
                },
                "$unset": {
                    "claim_token": "",
                    "claimed_at": "",
                    "claim_expires_at": "",
                    "last_error": "",
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return completed or {
            **referral,
            "rewarded": True,
            "status": "rewarded",
            "reward_days": reward_days,
            "reward_plan_id": reward_plan_id,
        }
    except Exception as exc:
        now = _utcnow()
        await collection().update_one(
            {"_id": referral["_id"], "claim_token": claim_token},
            {
                "$set": {
                    "status": "failed",
                    "last_error": str(exc)[:500],
                    "updated_at": now,
                },
                "$unset": {
                    "claim_token": "",
                    "claimed_at": "",
                    "claim_expires_at": "",
                },
            },
        )
        raise


async def set_seller_referral_settings(days: int, plan_id: str):
    clean_plan_id = str(plan_id).strip().lower()
    if not clean_plan_id:
        raise ValueError("Referral reward plan is required")
    return await update_config(
        seller_referral_reward_days=max(0, int(days)),
        seller_referral_reward_plan_id=clean_plan_id,
    )
