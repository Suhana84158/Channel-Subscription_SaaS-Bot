from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database.mongo import get_database
from utils.crypto import decrypt_secret, encrypt_secret

CONFIGS = "payment_gateway_configs"
TRANSACTIONS = "gateway_transactions"
EVENTS = "gateway_webhook_events"

SUPPORTED_GATEWAYS = ("razorpay", "cashfree")
SECRET_FIELDS = {
    "razorpay": {"key_secret", "webhook_secret"},
    "cashfree": {"client_secret"},
}
REQUIRED_FIELDS = {
    "razorpay": ("key_id", "key_secret", "webhook_secret"),
    "cashfree": ("client_id", "client_secret"),
}
VALID_MODES = {"test", "live"}


def gateway_missing_fields(gateway: str, settings: dict | None) -> list[str]:
    gateway = gateway.lower().strip()
    settings = settings or {}
    return [field for field in REQUIRED_FIELDS.get(gateway, ()) if not str(settings.get(field, "")).strip()]


def gateway_is_ready(gateway: str, settings: dict | None) -> bool:
    return gateway in SUPPORTED_GATEWAYS and not gateway_missing_fields(gateway, settings)


def _configs():
    return get_database()[CONFIGS]


def _transactions():
    return get_database()[TRANSACTIONS]


def _events():
    return get_database()[EVENTS]


async def initialize_payment_gateway_indexes():
    await _configs().create_index([("scope", 1), ("owner_id", 1)], unique=True)
    await _transactions().create_index("transaction_id", unique=True)
    await _transactions().create_index([("gateway", 1), ("gateway_order_id", 1)])
    await _transactions().create_index([("scope", 1), ("owner_id", 1), ("created_at", -1)])
    await _transactions().create_index([("status", 1), ("updated_at", -1)])
    await _events().create_index([("gateway", 1), ("event_key", 1)], unique=True)
    await _events().create_index("created_at", expireAfterSeconds=30 * 24 * 60 * 60)
    await _transactions().create_index([("status", 1), ("fulfillment_lease_until", 1)])
    await _transactions().create_index([("gateway_payment_id", 1)], sparse=True)


async def get_gateway_config(scope: str, owner_id: int = 0, decrypt: bool = False) -> dict:
    doc = await _configs().find_one({"scope": scope, "owner_id": int(owner_id)}) or {
        "scope": scope,
        "owner_id": int(owner_id),
        "default_gateway": "manual",
        "manual_enabled": True,
        "gateways": {},
    }
    if not decrypt:
        return doc
    result = dict(doc)
    gateways = {}
    for name, cfg in (doc.get("gateways") or {}).items():
        item = dict(cfg)
        for key in SECRET_FIELDS.get(name, set()):
            value = item.get(key)
            if value:
                try:
                    item[key] = decrypt_secret(value)
                except Exception:
                    item[key] = ""
        gateways[name] = item
    result["gateways"] = gateways
    return result


async def save_gateway_config(
    scope: str,
    owner_id: int,
    gateway: str,
    values: dict,
) -> dict:
    gateway = gateway.lower().strip()
    if gateway not in SUPPORTED_GATEWAYS:
        raise ValueError("Unsupported gateway")
    current = await get_gateway_config(scope, owner_id, decrypt=True)
    item = dict((current.get("gateways") or {}).get(gateway) or {})
    for key, value in values.items():
        if isinstance(value, str):
            value = value.strip()
        if key == "mode":
            # Automatic gateways always operate in live mode.
            value = "live"
        item[key] = value

    item["mode"] = "live"

    if item.get("enabled") and not gateway_is_ready(gateway, item):
        missing = ", ".join(gateway_missing_fields(gateway, item))
        raise ValueError(f"Set required credentials first: {missing}")

    encrypted = dict(item)
    for key in SECRET_FIELDS.get(gateway, set()):
        if encrypted.get(key):
            encrypted[key] = encrypt_secret(str(encrypted[key]))
    now = datetime.now(timezone.utc)
    await _configs().update_one(
        {"scope": scope, "owner_id": int(owner_id)},
        {
            "$set": {
                f"gateways.{gateway}": encrypted,
                "updated_at": now,
            },
            "$setOnInsert": {
                "scope": scope,
                "owner_id": int(owner_id),
                "default_gateway": "manual",
                "manual_enabled": True,
                "created_at": now,
            },
        },
        upsert=True,
    )
    return await get_gateway_config(scope, owner_id, decrypt=True)


async def set_gateway_preferences(
    scope: str,
    owner_id: int,
    *,
    default_gateway: str | None = None,
    manual_enabled: bool | None = None,
):
    updates = {"updated_at": datetime.now(timezone.utc)}
    if default_gateway is not None:
        if default_gateway != "manual" and default_gateway not in SUPPORTED_GATEWAYS:
            raise ValueError("Unsupported default gateway")
        updates["default_gateway"] = default_gateway
    if manual_enabled is not None:
        updates["manual_enabled"] = bool(manual_enabled)
    await _configs().update_one(
        {"scope": scope, "owner_id": int(owner_id)},
        {"$set": updates, "$setOnInsert": {"scope": scope, "owner_id": int(owner_id), "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return await get_gateway_config(scope, owner_id, decrypt=True)


async def mark_valid_webhook_received(scope: str, owner_id: int, gateway: str) -> None:
    """Record the latest successfully authenticated webhook for setup testing."""
    await _configs().update_one(
        {"scope": scope, "owner_id": int(owner_id)},
        {
            "$set": {
                f"gateways.{gateway}.last_webhook_received_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "scope": scope,
                "owner_id": int(owner_id),
                "default_gateway": "manual",
                "manual_enabled": True,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )


async def enabled_gateways(scope: str, owner_id: int = 0) -> list[str]:
    cfg = await get_gateway_config(scope, owner_id, decrypt=True)
    gateways = cfg.get("gateways") or {}
    return [
        name
        for name in SUPPORTED_GATEWAYS
        if gateways.get(name, {}).get("enabled") and gateway_is_ready(name, gateways.get(name))
    ]


async def create_gateway_transaction(
    *,
    scope: str,
    owner_id: int,
    payer_user_id: int,
    gateway: str,
    amount: float,
    currency: str,
    purpose: str,
    reference_id: str,
    metadata: dict,
) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "transaction_id": "gtw_" + uuid4().hex[:20],
        "scope": scope,
        "owner_id": int(owner_id),
        "payer_user_id": int(payer_user_id),
        "gateway": gateway,
        "amount": float(amount),
        "currency": currency.upper(),
        "purpose": purpose,
        "reference_id": str(reference_id),
        "metadata": metadata or {},
        "status": "created",
        "created_at": now,
        "updated_at": now,
    }
    await _transactions().insert_one(doc)
    return doc


async def update_gateway_transaction(transaction_id: str, **values) -> dict | None:
    values["updated_at"] = datetime.now(timezone.utc)
    return await _transactions().find_one_and_update(
        {"transaction_id": transaction_id},
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )


async def get_gateway_transaction(transaction_id: str) -> dict | None:
    return await _transactions().find_one({"transaction_id": transaction_id})


async def get_transaction_by_gateway_order(gateway: str, gateway_order_id: str) -> dict | None:
    return await _transactions().find_one({"gateway": gateway, "gateway_order_id": str(gateway_order_id)})


async def claim_transaction_success(transaction_id: str, gateway_payment_id: str, raw_event: dict | None = None) -> dict | None:
    now = datetime.now(timezone.utc)
    return await _transactions().find_one_and_update(
        {"transaction_id": transaction_id, "status": {"$nin": ["paid", "fulfilled"]}},
        {"$set": {"status": "paid", "gateway_payment_id": str(gateway_payment_id or ""), "paid_at": now, "raw_success": raw_event or {}, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )


async def claim_transaction_fulfillment(
    transaction_id: str,
    *,
    lease_seconds: int = 300,
) -> dict | None:
    """Claim exclusive fulfillment ownership for one paid transaction.

    A short lease allows recovery after a process crash while preventing two
    webhook workers from activating the same purchase at the same time.
    """
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=max(30, int(lease_seconds)))
    return await _transactions().find_one_and_update(
        {
            "transaction_id": transaction_id,
            "fulfilled_at": {"$exists": False},
            "$or": [
                {"status": {"$in": ["paid", "paid_unfulfilled"]}},
                {
                    "status": "fulfilling",
                    "fulfillment_lease_until": {"$lte": now},
                },
            ],
        },
        {
            "$set": {
                "status": "fulfilling",
                "fulfillment_started_at": now,
                "fulfillment_lease_until": lease_until,
                "updated_at": now,
            },
            "$inc": {"fulfillment_attempts": 1},
            "$unset": {"fulfillment_error": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


async def mark_transaction_fulfilled(
    transaction_id: str,
    details: dict | None = None,
) -> dict | None:
    now = datetime.now(timezone.utc)
    return await _transactions().find_one_and_update(
        {"transaction_id": transaction_id, "status": "fulfilling"},
        {
            "$set": {
                "status": "fulfilled",
                "fulfilled_at": now,
                "fulfillment": details or {},
                "updated_at": now,
            },
            "$unset": {"fulfillment_lease_until": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


async def mark_transaction_fulfillment_retry(
    transaction_id: str,
    reason: str,
) -> dict | None:
    """Release a failed fulfillment claim so a later webhook can retry it."""
    now = datetime.now(timezone.utc)
    return await _transactions().find_one_and_update(
        {"transaction_id": transaction_id, "status": "fulfilling"},
        {
            "$set": {
                "status": "paid_unfulfilled",
                "fulfillment_error": str(reason)[:500],
                "updated_at": now,
            },
            "$unset": {"fulfillment_lease_until": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


async def mark_transaction_failed(transaction_id: str, reason: str = "", raw_event: dict | None = None):
    return await update_gateway_transaction(transaction_id, status="failed", failure_reason=reason[:500], raw_failure=raw_event or {})


async def reserve_webhook_event(
    gateway: str,
    event_key: str,
    payload: dict | None = None,
) -> bool:
    try:
        await _events().insert_one({
            "gateway": gateway,
            "event_key": str(event_key),
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


async def claim_transaction_notification(transaction_id: str, notification: str) -> bool:
    """Atomically reserve a one-time post-payment notification/delivery.

    This is a second idempotency barrier for side effects such as subscriber
    invite delivery and seller notifications. It prevents duplicate messages
    even if the fulfillment function is reached twice by webhook/callback races.
    """
    name = str(notification or "").strip().lower()
    if not name.replace("_", "").isalnum():
        raise ValueError("Invalid notification name")
    field = f"notifications.{name}.claimed_at"
    now = datetime.now(timezone.utc)
    result = await _transactions().update_one(
        {
            "transaction_id": str(transaction_id),
            field: {"$exists": False},
        },
        {
            "$set": {
                field: now,
                f"notifications.{name}.status": "claimed",
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


async def complete_transaction_notification(transaction_id: str, notification: str, details: dict | None = None) -> None:
    name = str(notification or "").strip().lower()
    now = datetime.now(timezone.utc)
    await _transactions().update_one(
        {"transaction_id": str(transaction_id)},
        {
            "$set": {
                f"notifications.{name}.status": "sent",
                f"notifications.{name}.sent_at": now,
                f"notifications.{name}.details": details or {},
                "updated_at": now,
            }
        },
    )


async def fail_transaction_notification(transaction_id: str, notification: str, error: str) -> None:
    name = str(notification or "").strip().lower()
    now = datetime.now(timezone.utc)
    await _transactions().update_one(
        {"transaction_id": str(transaction_id)},
        {
            "$set": {
                f"notifications.{name}.status": "failed",
                f"notifications.{name}.error": str(error)[:500],
                "updated_at": now,
            }
        },
    )


async def recoverable_failed_notifications(limit: int = 100) -> list[dict]:
    """Return fulfilled child-subscription transactions whose invite delivery failed.

    Payment fulfillment and subscription activation are already complete for these
    records. Recovery must retry only the subscriber access message, never charge,
    extend validity, or create another invoice.
    """
    query = {
        "purpose": "child_subscription",
        "status": "fulfilled",
        "fulfilled_at": {"$exists": True},
        "notifications.subscriber_access.status": "failed",
    }
    safe_limit = max(1, min(int(limit), 500))
    return await _transactions().find(query).sort("updated_at", 1).limit(safe_limit).to_list(length=safe_limit)


async def reclaim_failed_transaction_notification(transaction_id: str, notification: str) -> bool:
    """Atomically reclaim one failed notification for a retry worker."""
    name = str(notification or "").strip().lower()
    if not name.replace("_", "").isalnum():
        raise ValueError("Invalid notification name")
    now = datetime.now(timezone.utc)
    status_field = f"notifications.{name}.status"
    result = await _transactions().update_one(
        {
            "transaction_id": str(transaction_id),
            status_field: "failed",
        },
        {
            "$set": {
                status_field: "claimed",
                f"notifications.{name}.claimed_at": now,
                "updated_at": now,
            },
            "$inc": {f"notifications.{name}.retry_count": 1},
            "$unset": {f"notifications.{name}.error": ""},
        },
    )
    return result.modified_count == 1


async def gateway_history(scope: str, owner_id: int, limit: int = 50) -> list[dict]:
    return await _transactions().find({"scope": scope, "owner_id": int(owner_id)}).sort("created_at", -1).to_list(length=limit)


async def recoverable_gateway_transactions(limit: int = 100) -> list[dict]:
    """Return paid transactions that can safely be fulfilled or retried."""
    now = datetime.now(timezone.utc)
    query = {
        "fulfilled_at": {"$exists": False},
        "$or": [
            {"status": {"$in": ["paid", "paid_unfulfilled"]}},
            {"status": "fulfilling", "fulfillment_lease_until": {"$lte": now}},
            {"status": "verification_pending", "updated_at": {"$lte": now - timedelta(minutes=2)}},
        ],
    }
    return await _transactions().find(query).sort("updated_at", 1).limit(max(1, min(int(limit), 500))).to_list(length=max(1, min(int(limit), 500)))


async def gateway_transaction_stats(scope: str, owner_id: int) -> dict:
    pipeline = [
        {"$match": {"scope": scope, "owner_id": int(owner_id)}},
        {"$group": {
            "_id": {"gateway": "$gateway", "status": "$status"},
            "count": {"$sum": 1},
            "amount": {"$sum": "$amount"},
        }},
    ]
    rows = await _transactions().aggregate(pipeline).to_list(length=None)
    result: dict[str, dict] = {}
    for row in rows:
        key = row.get("_id") or {}
        gateway = str(key.get("gateway") or "unknown")
        status = str(key.get("status") or "unknown")
        bucket = result.setdefault(gateway, {"total": 0, "amount": 0.0, "statuses": {}})
        count = int(row.get("count") or 0)
        amount = float(row.get("amount") or 0)
        bucket["total"] += count
        bucket["amount"] += amount
        bucket["statuses"][status] = {"count": count, "amount": amount}
    return result
