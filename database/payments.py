import asyncio
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from database.mongo import get_database

COLLECTION = "payments"
PENDING_PAYMENT_TTL_MINUTES = 30
FULFILLMENT_STALE_MINUTES = 15

_index_lock = asyncio.Lock()
_indexes_ready = False


def payments_collection():
    return get_database()[COLLECTION]


async def ensure_payment_indexes():
    """Create payment indexes once without changing existing records."""
    global _indexes_ready

    if _indexes_ready:
        return

    async with _index_lock:
        if _indexes_ready:
            return

        collection = payments_collection()

        # Only new pending records receive pending_key. The sparse unique
        # index therefore remains compatible with old payment history.
        await collection.create_index(
            [("pending_key", ASCENDING)],
            unique=True,
            sparse=True,
            name="unique_active_pending_payment",
        )
        await collection.create_index(
            [("status", ASCENDING), ("created_at", ASCENDING)],
            name="payment_status_created_at",
        )
        await collection.create_index(
            [("user_id", ASCENDING), ("created_at", ASCENDING)],
            name="payment_user_created_at",
        )
        await collection.create_index(
            [("status", ASCENDING), ("fulfillment_status", ASCENDING), ("updated_at", ASCENDING)],
            name="payment_fulfillment_recovery",
        )

        _indexes_ready = True


def _pending_key(user_id: int, plan: str) -> str:
    return f"{int(user_id)}:{str(plan).strip().lower()}"


async def expire_stale_pending_payments(
    user_id: int | None = None,
    *,
    ttl_minutes: int = PENDING_PAYMENT_TTL_MINUTES,
) -> int:
    """Expire old pending screenshot payments so they cannot be approved."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)

    query = {
        "status": "pending",
        "$or": [
            {"created_at": {"$lt": cutoff}},
            {"created_at": {"$exists": False}, "updated_at": {"$lt": cutoff}},
        ],
    }
    if user_id is not None:
        query["user_id"] = int(user_id)

    result = await payments_collection().update_many(
        query,
        {
            "$set": {
                "status": "expired",
                "remarks": "Payment session expired before approval.",
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"pending_key": ""},
        },
    )
    return result.modified_count


async def create_payment(
    user_id: int,
    plan: str,
    amount: float,
    screenshot_file_id: str = None,
    utr: str = None,
    duration_minutes: int = None,
    duration_text: str = None,
):
    """
    Create one active pending payment per user and plan.

    Repeated screenshot submissions update the existing pending payment rather
    than creating duplicate admin approval requests.
    """
    await ensure_payment_indexes()

    plan = str(plan or "").strip()
    if not plan:
        raise ValueError("Payment plan is required.")
    try:
        amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("Payment amount is invalid.") from exc
    if amount < 0:
        raise ValueError("Payment amount cannot be negative.")
    if duration_minutes is not None and int(duration_minutes) <= 0:
        raise ValueError("Payment duration must be positive.")
    utr = str(utr).strip()[:100] if utr is not None else None

    await expire_stale_pending_payments(user_id)

    now = datetime.now(timezone.utc)
    key = _pending_key(user_id, plan)

    payment_fields = {
        "user_id": int(user_id),
        "plan": plan,
        "amount": float(amount),
        "screenshot_file_id": screenshot_file_id,
        "utr": utr,
        "duration_minutes": duration_minutes,
        "duration_text": duration_text,
        "status": "pending",
        "admin_id": None,
        "remarks": None,
        "updated_at": now,
        "pending_key": key,
    }

    try:
        payment = await payments_collection().find_one_and_update(
            {"pending_key": key, "status": "pending"},
            {
                "$set": payment_fields,
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        # Another request won the race. Update and return that record.
        payment = await payments_collection().find_one_and_update(
            {"pending_key": key, "status": "pending"},
            {"$set": payment_fields},
            return_document=ReturnDocument.AFTER,
        )

    if payment is None:
        raise RuntimeError("Unable to create or update pending payment.")

    return payment


def to_object_id(payment_id):
    if isinstance(payment_id, ObjectId):
        return payment_id

    try:
        return ObjectId(str(payment_id))
    except (InvalidId, TypeError, ValueError) as exc:
        raise ValueError("Invalid payment ID.") from exc


async def get_payment(payment_id):
    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return None

    return await payments_collection().find_one({"_id": object_id})


async def get_pending_payments(limit: int = 20):
    await expire_stale_pending_payments()

    return await payments_collection().find(
        {"status": "pending"}
    ).sort("created_at", -1).to_list(length=limit)


async def get_payment_history(limit: int = 50):
    return await payments_collection().find(
        {"status": {"$in": ["approved", "rejected", "expired"]}}
    ).sort("updated_at", -1).to_list(length=limit)


async def decide_latest_payment(
    user_id: int,
    status: str,
    admin_id: int = None,
    remarks: str = None,
):
    payment = await payments_collection().find_one(
        {
            "user_id": int(user_id),
            "status": "pending",
        },
        sort=[("created_at", -1)],
    )

    if not payment:
        return None

    return await decide_payment_by_id(
        payment_id=payment["_id"],
        status=status,
        admin_id=admin_id,
        remarks=remarks,
    )


async def update_payment_status(
    user_id: int,
    status: str,
    admin_id: int = None,
    remarks: str = None,
):
    """Backward-compatible boolean wrapper for legacy callbacks."""
    payment = await decide_latest_payment(
        user_id=user_id,
        status=status,
        admin_id=admin_id,
        remarks=remarks,
    )
    return payment is not None


async def decide_payment_by_id(
    payment_id,
    status: str,
    admin_id: int = None,
    remarks: str = None,
):
    """
    Atomically decide one pending payment.

    Only the first admin action can move the payment away from ``pending``.
    The returned document is the winning decision. If another admin already
    decided it, ``None`` is returned.
    """
    if status not in {"approved", "rejected"}:
        raise ValueError("Payment status must be approved or rejected.")

    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    set_fields = {
        "status": status,
        "admin_id": admin_id,
        "remarks": remarks,
        "updated_at": now,
        "processed_at": now,
        "decision_status": status,
        "decision_admin_id": admin_id,
        "decision_at": now,
    }

    if status == "approved":
        set_fields.update(
            {
                "fulfillment_status": "pending",
                "fulfillment_attempts": 0,
                "fulfilled_channel_ids": [],
            }
        )
    else:
        set_fields.update(
            {
                "fulfillment_status": "not_required",
            }
        )

    return await payments_collection().find_one_and_update(
        {
            "_id": object_id,
            "status": "pending",
        },
        {
            "$set": set_fields,
            "$unset": {
                "pending_key": "",
                "fulfillment_claimed_at": "",
                "fulfillment_error": "",
            },
        },
        return_document=ReturnDocument.AFTER,
    )


async def update_payment_status_by_id(
    payment_id,
    status: str,
    admin_id: int = None,
    remarks: str = None,
):
    """Backward-compatible boolean wrapper around the atomic decision."""
    payment = await decide_payment_by_id(
        payment_id=payment_id,
        status=status,
        admin_id=admin_id,
        remarks=remarks,
    )
    return payment is not None


async def approve_payment(payment_id, admin_id: int):
    return await update_payment_status_by_id(
        payment_id=payment_id,
        status="approved",
        admin_id=admin_id,
    )


async def reject_payment(payment_id, admin_id: int, remarks: str = ""):
    return await update_payment_status_by_id(
        payment_id=payment_id,
        status="rejected",
        admin_id=admin_id,
        remarks=remarks,
    )


async def count_pending_payments():
    await expire_stale_pending_payments()
    return await payments_collection().count_documents(
        {"status": "pending"}
    )


async def total_revenue():
    pipeline = [
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]

    result = await payments_collection().aggregate(
        pipeline
    ).to_list(length=1)

    return result[0]["total"] if result else 0


async def total_payments():
    return await payments_collection().count_documents({})



async def get_latest_payment_for_user(
    user_id: int,
    *,
    status: str | None = None,
):
    query = {"user_id": int(user_id)}
    if status is not None:
        query["status"] = status

    return await payments_collection().find_one(
        query,
        sort=[("processed_at", -1), ("created_at", -1)],
    )


async def claim_payment_fulfillment(
    payment_id,
    *,
    admin_id: int | None = None,
    stale_minutes: int = FULFILLMENT_STALE_MINUTES,
):
    """
    Atomically claim an approved payment for fulfillment.

    Pending, failed, or stale processing records can be claimed. Completed
    records are never claimed again.
    """
    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(
        minutes=max(1, int(stale_minutes))
    )

    return await payments_collection().find_one_and_update(
        {
            "_id": object_id,
            "status": "approved",
            "$or": [
                {"fulfillment_status": {"$in": ["pending", "failed"]}},
                {"fulfillment_status": {"$exists": False}},
                {
                    "fulfillment_status": "processing",
                    "fulfillment_claimed_at": {"$lt": stale_before},
                },
            ],
        },
        {
            "$set": {
                "fulfillment_status": "processing",
                "fulfillment_claimed_at": now,
                "fulfillment_admin_id": admin_id,
                "updated_at": now,
            },
            "$inc": {"fulfillment_attempts": 1},
            "$unset": {"fulfillment_error": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


async def mark_payment_channel_delivered(
    payment_id,
    chat_id: int,
) -> bool:
    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return False

    result = await payments_collection().update_one(
        {
            "_id": object_id,
            "status": "approved",
            "fulfillment_status": "processing",
        },
        {
            "$addToSet": {
                "fulfilled_channel_ids": int(chat_id),
            },
            "$set": {
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )
    return result.matched_count == 1


async def mark_payment_subscription_fulfilled(
    payment_id,
    *,
    expiry,
    action: str,
) -> bool:
    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return False

    result = await payments_collection().update_one(
        {
            "_id": object_id,
            "status": "approved",
            "fulfillment_status": "processing",
        },
        {
            "$set": {
                "subscription_fulfilled": True,
                "subscription_action": str(action),
                "subscription_expiry": expiry,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return result.matched_count == 1


async def complete_payment_fulfillment(
    payment_id,
) -> bool:
    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return False

    now = datetime.now(timezone.utc)
    result = await payments_collection().update_one(
        {
            "_id": object_id,
            "status": "approved",
            "fulfillment_status": "processing",
        },
        {
            "$set": {
                "fulfillment_status": "completed",
                "fulfilled_at": now,
                "updated_at": now,
            },
            "$unset": {
                "fulfillment_claimed_at": "",
                "fulfillment_error": "",
            },
        },
    )
    return result.modified_count == 1


async def fail_payment_fulfillment(
    payment_id,
    error: str,
) -> bool:
    try:
        object_id = to_object_id(payment_id)
    except ValueError:
        return False

    now = datetime.now(timezone.utc)
    result = await payments_collection().update_one(
        {
            "_id": object_id,
            "status": "approved",
            "fulfillment_status": "processing",
        },
        {
            "$set": {
                "fulfillment_status": "failed",
                "fulfillment_error": str(error)[:1000],
                "fulfillment_failed_at": now,
                "updated_at": now,
            },
            "$unset": {"fulfillment_claimed_at": ""},
        },
    )
    return result.modified_count == 1


async def recover_orphaned_payments(
    *,
    pending_ttl_minutes: int = PENDING_PAYMENT_TTL_MINUTES,
    fulfillment_stale_minutes: int = FULFILLMENT_STALE_MINUTES,
) -> dict[str, int]:
    """Repair payment records left incomplete by a crash or redeploy.

    This operation is idempotent and does not fulfill subscriptions itself.
    It only makes incomplete records safe for the normal approval/retry flow.
    """
    await ensure_payment_indexes()

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(
        minutes=max(1, int(fulfillment_stale_minutes))
    )

    expired_pending = await expire_stale_pending_payments(
        ttl_minutes=max(1, int(pending_ttl_minutes))
    )

    stale_processing = await payments_collection().update_many(
        {
            "status": "approved",
            "fulfillment_status": "processing",
            "$or": [
                {"fulfillment_claimed_at": {"$lt": stale_before}},
                {"fulfillment_claimed_at": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "fulfillment_status": "failed",
                "fulfillment_error": (
                    "Recovered stale payment fulfillment after restart."
                ),
                "fulfillment_failed_at": now,
                "updated_at": now,
            },
            "$unset": {"fulfillment_claimed_at": ""},
        },
    )

    legacy_approved = await payments_collection().update_many(
        {
            "status": "approved",
            "fulfillment_status": {"$exists": False},
        },
        {
            "$set": {
                "fulfillment_status": "pending",
                "fulfillment_attempts": 0,
                "fulfilled_channel_ids": [],
                "updated_at": now,
            }
        },
    )

    orphan_pending_keys = await payments_collection().update_many(
        {
            "status": {"$ne": "pending"},
            "pending_key": {"$exists": True},
        },
        {
            "$unset": {"pending_key": ""},
            "$set": {"updated_at": now},
        },
    )

    invalid_pending_keys = await payments_collection().update_many(
        {
            "status": "pending",
            "$or": [
                {"user_id": {"$exists": False}},
                {"plan": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "status": "expired",
                "remarks": "Invalid pending payment record recovered.",
                "updated_at": now,
            },
            "$unset": {"pending_key": ""},
        },
    )

    return {
        "expired_pending": int(expired_pending),
        "stale_processing": int(stale_processing.modified_count),
        "legacy_approved": int(legacy_approved.modified_count),
        "orphan_pending_keys": int(orphan_pending_keys.modified_count),
        "invalid_pending": int(invalid_pending_keys.modified_count),
    }
