from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from database.mongo import get_database

AUDIT = "platform_audit_logs"
SCHEDULED = "seller_scheduled_broadcasts"
FAILED_DELIVERY = "seller_failed_deliveries"
SELLER_COUPONS = "seller_coupons"
INVOICES = "seller_invoices"
POLICIES = "platform_policies"
PAYMENT_FINGERPRINTS = "payment_fingerprints"
REFERRAL_LEDGER = "seller_referral_commissions"


def col(name: str):
    return get_database()[name]


async def initialize_platform_feature_indexes():
    await col(AUDIT).create_index([("created_at", -1)])
    await col(AUDIT).create_index([("owner_id", 1), ("created_at", -1)])
    await col(SCHEDULED).create_index([("owner_id", 1), ("status", 1), ("run_at", 1)])
    await col(FAILED_DELIVERY).create_index([("owner_id", 1), ("kind", 1), ("resolved", 1)])
    await col(SELLER_COUPONS).create_index([("owner_id", 1), ("code", 1)], unique=True)
    await col(INVOICES).create_index([("owner_id", 1), ("invoice_no", 1)], unique=True)
    await col(PAYMENT_FINGERPRINTS).create_index([("scope", 1), ("owner_id", 1), ("fingerprint", 1)], unique=True)
    await col(REFERRAL_LEDGER).create_index([("seller_id", 1), ("created_at", -1)])
    await col(POLICIES).create_index("key", unique=True)
    now = datetime.now(timezone.utc)
    defaults = {
        "terms": "By using this service, users and sellers agree to follow Telegram rules and provide accurate payment information.",
        "privacy": "Only data needed to run subscriptions, payments and support is stored. Data is not sold.",
        "refund": "Refund decisions are handled by the relevant seller. Contact support with payment details.",
        "support": "Contact the platform owner through the support button in the main bot.",
    }
    for key, text in defaults.items():
        await col(POLICIES).update_one({"key": key}, {"$setOnInsert": {"key": key, "text": text, "updated_at": now}}, upsert=True)


async def audit(action: str, actor_id: int, owner_id: int | None = None, details: dict[str, Any] | None = None):
    await col(AUDIT).insert_one({
        "action": action,
        "actor_id": int(actor_id),
        "owner_id": int(owner_id) if owner_id is not None else None,
        "details": details or {},
        "created_at": datetime.now(timezone.utc),
    })


async def recent_audit(limit: int = 30):
    return await col(AUDIT).find({}).sort("created_at", -1).to_list(length=limit)


async def reserve_payment_fingerprint(scope: str, owner_id: int, fingerprint: str, user_id: int) -> bool:
    if not fingerprint:
        return True
    try:
        await col(PAYMENT_FINGERPRINTS).insert_one({
            "scope": scope,
            "owner_id": int(owner_id),
            "fingerprint": fingerprint,
            "user_id": int(user_id),
            "created_at": datetime.now(timezone.utc),
        })
        return True
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "E11000" in str(exc):
            return False
        raise


async def release_payment_fingerprint(
    scope: str,
    owner_id: int,
    fingerprint: str,
    user_id: int | None = None,
):
    """Release a reserved fingerprint when payment creation fails."""
    if not fingerprint:
        return

    query = {
        "scope": scope,
        "owner_id": int(owner_id),
        "fingerprint": fingerprint,
    }
    if user_id is not None:
        query["user_id"] = int(user_id)

    await col(PAYMENT_FINGERPRINTS).delete_one(query)


async def create_invoice(owner_id: int, user_id: int, payment: dict, seller_name: str = "Seller"):
    invoice_no = f"INV-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:8].upper()}"
    doc = {
        "owner_id": int(owner_id), "user_id": int(user_id), "invoice_no": invoice_no,
        "seller_name": seller_name, "plan": payment.get("plan", "Plan"),
        "amount": float(payment.get("amount", 0) or 0), "payment_id": payment.get("payment_id"),
        "created_at": datetime.now(timezone.utc),
    }
    await col(INVOICES).insert_one(doc)
    return doc


async def get_invoice(owner_id: int, invoice_no: str):
    return await col(INVOICES).find_one({"owner_id": int(owner_id), "invoice_no": invoice_no})


async def save_failed_delivery(owner_id: int, user_id: int, kind: str, payload: dict | None = None, error: str = ""):
    await col(FAILED_DELIVERY).update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id), "kind": kind, "resolved": False},
        {"$set": {"payload": payload or {}, "error": error[:500], "updated_at": datetime.now(timezone.utc)},
         "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True)


async def get_failed_deliveries(owner_id: int, kind: str):
    return await col(FAILED_DELIVERY).find({"owner_id": int(owner_id), "kind": kind, "resolved": False}).to_list(length=None)


async def resolve_failed_delivery(doc_id):
    await col(FAILED_DELIVERY).update_one({"_id": doc_id}, {"$set": {"resolved": True, "resolved_at": datetime.now(timezone.utc)}})


async def create_coupon(owner_id: int, code: str, discount_type: str, value: float, usage_limit: int, expiry=None):
    doc = {"owner_id": int(owner_id), "code": code.strip().upper(), "discount_type": discount_type,
           "value": float(value), "usage_limit": int(usage_limit), "used_count": 0, "active": True,
           "expiry": expiry, "created_at": datetime.now(timezone.utc)}
    await col(SELLER_COUPONS).update_one({"owner_id": int(owner_id), "code": doc["code"]}, {"$set": doc}, upsert=True)
    return doc


async def list_coupons(owner_id: int):
    return await col(SELLER_COUPONS).find({"owner_id": int(owner_id)}).sort("created_at", -1).to_list(length=100)


async def validate_coupon(owner_id: int, code: str, amount: float):
    coupon = await col(SELLER_COUPONS).find_one({"owner_id": int(owner_id), "code": code.strip().upper(), "active": True})
    if not coupon:
        return None, "Invalid coupon"
    now = datetime.now(timezone.utc)
    expiry = coupon.get("expiry")
    if expiry and expiry < now:
        return None, "Coupon expired"
    if int(coupon.get("used_count", 0)) >= int(coupon.get("usage_limit", 0)):
        return None, "Coupon usage limit reached"
    value = float(coupon.get("value", 0))
    discount = amount * value / 100 if coupon.get("discount_type") == "percent" else value
    return max(0.0, amount - discount), None


async def use_coupon(owner_id: int, code: str):
    await col(SELLER_COUPONS).update_one({"owner_id": int(owner_id), "code": code.strip().upper()}, {"$inc": {"used_count": 1}})


async def save_scheduled_broadcast(owner_id: int, run_at, from_chat_id: int, message_id: int, audience: str = "all"):
    doc = {"job_id": uuid4().hex, "owner_id": int(owner_id), "run_at": run_at, "from_chat_id": int(from_chat_id),
           "message_id": int(message_id), "audience": audience, "status": "pending", "created_at": datetime.now(timezone.utc)}
    await col(SCHEDULED).insert_one(doc)
    return doc


async def pending_scheduled_broadcasts(owner_id: int, limit: int = 100):
    """Return pending broadcasts so clone-bot restarts can restore JobQueue jobs."""
    return await col(SCHEDULED).find(
        {
            "owner_id": int(owner_id),
            "status": "pending",
        }
    ).sort("run_at", 1).to_list(length=limit)


async def claim_scheduled_broadcast(job_id: str) -> bool:
    """
    Atomically move one scheduled broadcast from pending to processing.

    This prevents duplicate delivery when a restored job and an old in-memory
    JobQueue job fire at the same time.
    """
    now = datetime.now(timezone.utc)
    result = await col(SCHEDULED).update_one(
        {
            "job_id": job_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "processing",
                "started_at": now,
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


async def release_scheduled_broadcast(job_id: str, error: str):
    """Return a failed startup-level job to pending for a future retry."""
    await col(SCHEDULED).update_one(
        {
            "job_id": job_id,
            "status": "processing",
        },
        {
            "$set": {
                "status": "pending",
                "last_error": str(error)[:1000],
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"started_at": ""},
        },
    )


async def set_scheduled_status(job_id: str, status: str, result: dict | None = None):
    update = {
        "$set": {
            "status": status,
            "result": result or {},
            "updated_at": datetime.now(timezone.utc),
        }
    }
    if status != "processing":
        update["$unset"] = {"started_at": ""}
    await col(SCHEDULED).update_one({"job_id": job_id}, update)


async def get_policy(key: str):
    return await col(POLICIES).find_one({"key": key}) or {"key": key, "text": "Not configured."}


async def set_policy(key: str, text: str):
    await col(POLICIES).update_one({"key": key}, {"$set": {"text": text, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
