import asyncio
import time
from datetime import datetime, timedelta, timezone
from database.mongo import get_database
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

SETTINGS = "seller_subscription_settings"
ASSIGNMENTS = "seller_plan_assignments"

DEFAULT_FREE = {
    "name": "Free",
    "price": 0.0,
    "duration_days": 0,
    "bot_limit": 1,
    "active_subscriber_limit": 25,
    "channel_limit": 1,
    "plan_limit": 2,
    "admin_limit": 1,
    "broadcast_enabled": False,
    "coupon_enabled": False,
    "referral_enabled": False,
    "analytics_enabled": False,
    "branding_enabled": True,
}

DEFAULT_PAID = [
    {"plan_id": "starter", "name": "Starter", "price": 299.0, "duration_days": 30,
     "bot_limit": 3, "active_subscriber_limit": 250, "channel_limit": 3,
     "plan_limit": 10, "admin_limit": 2, "broadcast_enabled": True,
     "coupon_enabled": True, "referral_enabled": True, "analytics_enabled": True,
     "branding_enabled": True, "active": True},
    {"plan_id": "professional", "name": "Professional", "price": 699.0, "duration_days": 30,
     "bot_limit": 10, "active_subscriber_limit": 2000, "channel_limit": 10,
     "plan_limit": 30, "admin_limit": 5, "broadcast_enabled": True,
     "coupon_enabled": True, "referral_enabled": True, "analytics_enabled": True,
     "branding_enabled": True, "active": True},
    {"plan_id": "business", "name": "Business", "price": 1499.0, "duration_days": 30,
     "bot_limit": 30, "active_subscriber_limit": 10000, "channel_limit": 30,
     "plan_limit": 100, "admin_limit": 10, "broadcast_enabled": True,
     "coupon_enabled": True, "referral_enabled": True, "analytics_enabled": True,
     "branding_enabled": True, "active": True},
]


def _settings():
    return get_database()[SETTINGS]


def _assignments():
    return get_database()[ASSIGNMENTS]


_CONFIG_CACHE_TTL_SECONDS = 15.0
_config_cache: dict | None = None
_config_cache_at = 0.0
_defaults_ready = False
_defaults_lock = asyncio.Lock()


async def _ensure_defaults_once():
    global _defaults_ready
    if _defaults_ready:
        return
    async with _defaults_lock:
        if _defaults_ready:
            return
        await initialize_defaults()
        _defaults_ready = True


async def initialize_seller_subscription_indexes():
    await _settings().create_index("key", unique=True)
    await _assignments().create_index("owner_id", unique=True)
    await initialize_defaults()
    await initialize_seller_subscription_extra_indexes()


async def initialize_defaults():
    now = datetime.now(timezone.utc)
    await _settings().update_one(
        {"key": "config"},
        {"$setOnInsert": {
            "key": "config",
            "free_plan": DEFAULT_FREE,
            "paid_plans": DEFAULT_PAID,
            "trial_enabled": True,
            "trial_days": 7,
            "trial_plan_id": "starter",
            "payment_upi_id": "",
            "payment_upi_name": "",
            "payment_qr_file_id": "",
            "branding_text": "Powered by Subscription SaaS Bot",
            "created_at": now,
            "updated_at": now,
        }},
        upsert=True,
    )


async def get_config(force_refresh: bool = False):
    global _config_cache, _config_cache_at
    await _ensure_defaults_once()
    now = time.monotonic()
    if (
        not force_refresh
        and _config_cache is not None
        and now - _config_cache_at < _CONFIG_CACHE_TTL_SECONDS
    ):
        return dict(_config_cache)

    config = await _settings().find_one({"key": "config"}) or {}
    _config_cache = dict(config)
    _config_cache_at = now
    return dict(config)


async def update_config(**values):
    global _config_cache, _config_cache_at
    values["updated_at"] = datetime.now(timezone.utc)
    await _settings().update_one({"key": "config"}, {"$set": values}, upsert=True)
    _config_cache = None
    _config_cache_at = 0.0
    return await get_config(force_refresh=True)


async def get_paid_plan(plan_id: str):
    config = await get_config()
    return next((p for p in config.get("paid_plans", []) if p.get("plan_id") == plan_id), None)


async def save_paid_plan(plan: dict):
    config = await get_config()
    plans = list(config.get("paid_plans", []))
    idx = next((i for i, p in enumerate(plans) if p.get("plan_id") == plan.get("plan_id")), None)
    if idx is None:
        plans.append(plan)
    else:
        plans[idx] = {**plans[idx], **plan}
    # Branding is always ON on free and paid plans.
    for p in plans:
        p["branding_enabled"] = True
    await update_config(paid_plans=plans)
    return plan


async def delete_paid_plan(plan_id: str):
    config = await get_config()
    plans = [p for p in config.get("paid_plans", []) if p.get("plan_id") != plan_id]
    await update_config(paid_plans=plans)


async def get_assignment(owner_id: int):
    return await _assignments().find_one({"owner_id": int(owner_id)})


async def assign_plan(owner_id: int, plan_id: str, days: int | None = None, source="owner"):
    now = datetime.now(timezone.utc)
    # A custom paid plan may also be named/id-ed "free". Gateway purchases
    # pass a positive duration, so do not confuse that paid plan with the
    # platform's permanent free tier.
    is_permanent_free_tier = plan_id == "free" and days is None
    if is_permanent_free_tier:
        expiry = None
    else:
        plan = await get_paid_plan(plan_id)
        if not plan:
            raise ValueError("Paid plan not found")
        duration = int(days or plan.get("duration_days", 30))
        if duration <= 0:
            raise ValueError("Plan duration must be greater than zero")
        expiry = now + timedelta(days=duration)
    await _assignments().update_one(
        {"owner_id": int(owner_id)},
        {"$set": {"plan_id": plan_id, "expiry_date": expiry, "source": source,
                  "updated_at": now},
         "$setOnInsert": {"owner_id": int(owner_id), "created_at": now}},
        upsert=True,
    )
    return await get_assignment(owner_id)


async def start_trial(owner_id: int):
    config = await get_config()
    if not config.get("trial_enabled", True):
        raise ValueError("Free trial is disabled")
    existing = await get_assignment(owner_id)
    if existing and existing.get("trial_used"):
        raise ValueError("Free trial already used")
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=int(config.get("trial_days", 7)))
    await _assignments().update_one(
        {"owner_id": int(owner_id)},
        {"$set": {"plan_id": config.get("trial_plan_id", "starter"),
                  "expiry_date": expiry, "trial_used": True, "source": "trial",
                  "updated_at": now},
         "$setOnInsert": {"owner_id": int(owner_id), "created_at": now}},
        upsert=True,
    )
    return await get_assignment(owner_id)


async def effective_plan(owner_id: int):
    config = await get_config()
    free = dict(config.get("free_plan") or DEFAULT_FREE)
    free["plan_id"] = "free"
    free["branding_enabled"] = True
    assignment = await get_assignment(owner_id)
    if not assignment:
        return free, None
    expiry = assignment.get("expiry_date")
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry and expiry <= datetime.now(timezone.utc):
        return free, assignment
    plan_id = assignment.get("plan_id", "free")
    if plan_id == "free":
        return free, assignment
    paid = await get_paid_plan(plan_id)
    if not paid or not paid.get("active", True):
        return free, assignment
    paid = dict(paid)
    paid["branding_enabled"] = True
    return paid, assignment


async def limit_for(owner_id: int, key: str, default=0):
    plan, _ = await effective_plan(owner_id)
    return plan.get(key, default)


def _limit_text(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value)
    return "Unlimited" if value < 0 else f"{value:,}"


async def plan_limit_warning(owner_id: int):
    plan, _ = await effective_plan(owner_id)
    name = str(plan.get("name") or "Free").strip()
    return (
        f"⚠️ {name} Plan Limit Reached\n\n"
        f"Your {name} plan supports:\n\n"
        f"• {_limit_text(plan.get('bot_limit', 1))} Clone Bot"
        f"{'s' if int(plan.get('bot_limit', 1) or 0) != 1 else ''}\n"
        f"• {_limit_text(plan.get('active_subscriber_limit', 25))} Active Subscribers\n"
        f"• {_limit_text(plan.get('channel_limit', 1))} Channel/Group"
        f"{'s' if int(plan.get('channel_limit', 1) or 0) != 1 else ''}\n"
        f"• {_limit_text(plan.get('plan_limit', 2))} Subscription Plans\n\n"
        "Upgrade your seller plan to continue."
    )


async def seller_usage(owner_id: int):
    from database.seller_bots import count_owner_bots
    from database.seller_data import active_subscriptions, get_channels, get_plans

    bot_count, subscriptions, channels, plans = await asyncio.gather(
        count_owner_bots(owner_id),
        active_subscriptions(owner_id),
        get_channels(owner_id),
        get_plans(owner_id),
    )
    return {
        "bot_count": bot_count,
        "active_subscriber_count": len(subscriptions),
        "channel_count": len(channels),
        "plan_count": len(plans),
    }


async def current_plan_text(owner_id: int):
    plan, assignment = await effective_plan(owner_id)
    usage = await seller_usage(owner_id)
    def row(label, used, key):
        return f"{label}: {used:,} / {_limit_text(plan.get(key, 0))}"
    status = "✅ Within plan limits"
    checks = [
        (usage['bot_count'], plan.get('bot_limit', 1)),
        (usage['active_subscriber_count'], plan.get('active_subscriber_limit', 25)),
        (usage['channel_count'], plan.get('channel_limit', 1)),
        (usage['plan_count'], plan.get('plan_limit', 2)),
    ]
    if any(int(limit) >= 0 and used >= int(limit) for used, limit in checks):
        status = "⚠️ One or more plan limits reached"
    return (
        "📊 Current Seller Plan\n\n"
        f"Plan: {plan.get('name', 'Free')}\n\n"
        "Usage\n\n"
        f"{row('🤖 Clone Bots', usage['bot_count'], 'bot_limit')}\n"
        f"{row('👥 Active Subscribers', usage['active_subscriber_count'], 'active_subscriber_limit')}\n"
        f"{row('📢 Channels/Groups', usage['channel_count'], 'channel_limit')}\n"
        f"{row('📦 Subscription Plans', usage['plan_count'], 'plan_limit')}\n\n"
        f"Status: {status}"
    )

PAYMENTS = "seller_plan_payments"
HISTORY = "seller_plan_history"
REQUESTS = "seller_plan_requests"


def _payments():
    return get_database()[PAYMENTS]


def _history():
    return get_database()[HISTORY]


def _requests():
    return get_database()[REQUESTS]


async def initialize_seller_subscription_extra_indexes():
    await _payments().create_index("payment_id", unique=True)
    await _payments().create_index(
        "pending_key",
        unique=True,
        sparse=True,
        name="unique_pending_seller_plan_payment",
    )
    await _payments().create_index([("status", 1), ("created_at", -1)])
    await _history().create_index([("owner_id", 1), ("created_at", -1)])
    await _requests().create_index([("owner_id", 1), ("status", 1)])


async def record_history(owner_id: int, action: str, **details):
    doc = {"owner_id": int(owner_id), "action": action, "created_at": datetime.now(timezone.utc), **details}
    await _history().insert_one(doc)
    return doc


async def subscription_history(owner_id: int | None = None, limit: int = 30):
    query = {"owner_id": int(owner_id)} if owner_id is not None else {}
    return await _history().find(query).sort("created_at", -1).limit(limit).to_list(length=limit)


async def assign_plan_with_history(owner_id: int, plan_id: str, days: int | None = None, source="owner", amount: float = 0, approved_by: int | None = None):
    before, _ = await effective_plan(owner_id)
    assignment = await assign_plan(owner_id, plan_id, days, source)
    after, _ = await effective_plan(owner_id)
    await record_history(
        owner_id, "plan_assigned", previous_plan=before.get("plan_id", "free"),
        new_plan=after.get("plan_id", plan_id), days=days, source=source,
        amount=float(amount or 0), approved_by=approved_by,
        expiry_date=assignment.get("expiry_date"),
    )
    return assignment


async def extend_plan_with_history(owner_id: int, plan_id: str, days: int, source="owner", approved_by: int | None = None):
    """Extend a seller plan from the current expiry (or now if expired)."""
    days=int(days)
    if days <= 0:
        raise ValueError("Days must be greater than zero")
    plan=await get_paid_plan(plan_id)
    if not plan:
        raise ValueError("Paid plan not found")
    now=datetime.now(timezone.utc)
    current=await get_assignment(owner_id) or {}
    current_expiry=current.get("expiry_date")
    if current_expiry and current_expiry.tzinfo is None:
        current_expiry=current_expiry.replace(tzinfo=timezone.utc)
    base=current_expiry if current_expiry and current_expiry > now else now
    new_expiry=base + timedelta(days=days)
    previous_plan=current.get("plan_id", "free")
    await _assignments().update_one(
        {"owner_id": int(owner_id)},
        {"$set": {
            "plan_id": plan_id,
            "expiry_date": new_expiry,
            "source": source,
            "subscription_suspended": False,
            "suspension_reason": "",
            "updated_at": now,
        }, "$setOnInsert": {"owner_id": int(owner_id), "created_at": now}},
        upsert=True,
    )
    await record_history(
        owner_id, "plan_extended", previous_plan=previous_plan,
        new_plan=plan_id, days=days, source=source,
        approved_by=approved_by, expiry_date=new_expiry,
    )
    return await get_assignment(owner_id)


async def create_seller_payment(owner_id: int, plan_id: str, file_id: str, request_type="upgrade"):
    """
    Create or update one active pending seller-plan payment.

    Repeated submissions for the same seller and plan update the pending proof
    instead of creating multiple approval records.
    """
    import secrets

    owner_id = int(owner_id)
    plan = await get_paid_plan(plan_id)
    if not plan:
        raise ValueError("Plan not found")

    now = datetime.now(timezone.utc)
    pending_key = f"{owner_id}:{plan_id}"
    payment_id = secrets.token_hex(6)

    update = {
        "$set": {
            "owner_id": owner_id,
            "plan_id": plan_id,
            "plan_name": plan.get("name", plan_id),
            "amount": float(plan.get("price", 0)),
            "duration_days": int(plan.get("duration_days", 30)),
            "file_id": file_id,
            "request_type": request_type,
            "status": "pending",
            "pending_key": pending_key,
            "updated_at": now,
        },
        "$setOnInsert": {
            "payment_id": payment_id,
            "created_at": now,
        },
    }

    try:
        doc = await _payments().find_one_and_update(
            {"pending_key": pending_key, "status": "pending"},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        doc = await _payments().find_one_and_update(
            {"pending_key": pending_key, "status": "pending"},
            {"$set": update["$set"]},
            return_document=ReturnDocument.AFTER,
        )

    if not doc:
        raise RuntimeError("Unable to create seller payment request.")

    return doc


async def get_seller_payment(payment_id: str):
    return await _payments().find_one({"payment_id": payment_id})


async def pending_seller_payments(limit=50):
    return await _payments().find({"status": "pending"}).sort("created_at", 1).limit(limit).to_list(length=limit)


async def decide_seller_payment(payment_id: str, status: str, admin_id: int):
    now = datetime.now(timezone.utc)
    result = await _payments().find_one_and_update(
        {"payment_id": payment_id, "status": "pending"},
        {
            "$set": {
                "status": status,
                "decided_at": now,
                "decided_by": int(admin_id),
                "updated_at": now,
            },
            "$unset": {"pending_key": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    return result


async def create_plan_request(owner_id: int, target_plan_id: str, request_type: str):
    now = datetime.now(timezone.utc)
    await _requests().update_many({"owner_id": int(owner_id), "status": "pending"}, {"$set": {"status": "superseded"}})
    doc = {"owner_id": int(owner_id), "target_plan_id": target_plan_id, "request_type": request_type, "status": "pending", "created_at": now}
    await _requests().insert_one(doc)
    await record_history(owner_id, f"{request_type}_requested", target_plan_id=target_plan_id)
    return doc


async def seller_revenue_summary():
    pipeline = [
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]
    rows = await _payments().aggregate(pipeline).to_list(length=1)
    total = float(rows[0]["total"]) if rows else 0.0
    count = int(rows[0]["count"]) if rows else 0
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    month_rows = await _payments().aggregate([
        {"$match": {"status": "approved", "decided_at": {"$gte": month_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]).to_list(length=1)
    return {"total": total, "count": count, "month_total": float(month_rows[0]["total"]) if month_rows else 0.0, "month_count": int(month_rows[0]["count"]) if month_rows else 0}


async def set_subscription_suspension(owner_id: int, suspended: bool, reason=""):
    await _assignments().update_one(
        {"owner_id": int(owner_id)},
        {"$set": {"subscription_suspended": bool(suspended), "suspension_reason": reason, "updated_at": datetime.now(timezone.utc)}, "$setOnInsert": {"owner_id": int(owner_id), "plan_id": "free", "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await record_history(owner_id, "suspended" if suspended else "unsuspended", reason=reason)


async def seller_access_state(owner_id: int):
    assignment = await get_assignment(owner_id)
    now = datetime.now(timezone.utc)
    if assignment and assignment.get("subscription_suspended"):
        return {"allowed": False, "reason": "suspended", "message": "🚫 Your seller subscription is suspended. Contact the SaaS owner."}
    expiry = assignment.get("expiry_date") if assignment else None
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry and expiry <= now:
        return {"allowed": False, "reason": "expired", "message": "⛔ Your paid seller plan has expired. Existing subscribers keep access, but new payments, users, plans and channels are restricted until renewal."}
    return {"allowed": True, "reason": "active", "message": ""}


async def usage_warning(owner_id: int, threshold=0.8):
    plan, _ = await effective_plan(owner_id)
    usage = await seller_usage(owner_id)
    checks = [
        ("Clone Bots", usage["bot_count"], plan.get("bot_limit", 1)),
        ("Active Subscribers", usage["active_subscriber_count"], plan.get("active_subscriber_limit", 25)),
        ("Channels/Groups", usage["channel_count"], plan.get("channel_limit", 1)),
        ("Subscription Plans", usage["plan_count"], plan.get("plan_limit", 2)),
    ]
    warnings=[]
    for label, used, limit in checks:
        try: limit=int(limit)
        except Exception: continue
        if limit > 0 and used/limit >= threshold:
            warnings.append(f"• {label}: {used:,} / {limit:,} ({int(used/limit*100)}%)")
    if not warnings: return None
    return "⚠️ Plan Usage Warning\n\n" + "\n".join(warnings) + "\n\nUpgrade before reaching the limit."

async def bot_runtime_allowed(owner_id: int, bot_id: int):
    """Return whether this clone bot is inside the seller plan's runtime quota.

    Bots are ranked by creation time, then bot_id, so the decision is stable
    across restarts. A negative limit means unlimited.
    """
    from database.seller_bots import get_bots

    owner_id = int(owner_id)
    bot_id = int(bot_id)
    plan, _ = await effective_plan(owner_id)
    try:
        limit = int(plan.get("bot_limit", 1))
    except (TypeError, ValueError):
        limit = 1

    if limit < 0:
        return True, {"limit": limit, "position": 1, "plan": plan}
    if limit == 0:
        return False, {"limit": 0, "position": None, "plan": plan}

    records = await get_bots(owner_id)
    records = sorted(
        records,
        key=lambda item: (item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), int(item.get("bot_id", 0))),
    )
    for position, record in enumerate(records, start=1):
        if int(record.get("bot_id", 0)) == bot_id:
            return position <= limit, {"limit": limit, "position": position, "plan": plan}

    return False, {"limit": limit, "position": None, "plan": plan}


def normalize_plan_limit(value, default: int = 0) -> int:
    """Return a safe integer limit. -1 means unlimited."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = int(default)
    return max(-1, value)


async def resource_limit_status(owner_id: int, resource: str):
    """Return current usage and whether another resource may be created."""
    plan, _ = await effective_plan(int(owner_id))
    usage = await seller_usage(int(owner_id))
    mapping = {
        "bot": ("bot_count", "bot_limit", 1),
        "subscriber": ("active_subscriber_count", "active_subscriber_limit", 25),
        "channel": ("channel_count", "channel_limit", 1),
        "plan": ("plan_count", "plan_limit", 2),
        "admin": ("admin_count", "admin_limit", 1),
    }
    if resource not in mapping:
        raise ValueError(f"Unknown resource: {resource}")
    usage_key, limit_key, default = mapping[resource]
    used = int(usage.get(usage_key, 0) or 0)
    limit = normalize_plan_limit(plan.get(limit_key), default)
    return {
        "allowed": limit < 0 or used < limit,
        "used": used,
        "limit": limit,
        "plan": plan,
        "resource": resource,
    }


def validate_plan_limits(*values: int):
    """Validate owner-configured limits. -1 means unlimited; lower values are invalid."""
    parsed = []
    for value in values:
        number = int(value)
        if number < -1:
            raise ValueError("Limits must be -1 (Unlimited), 0, or a positive number")
        parsed.append(number)
    return parsed


async def expiring_assignments(days_ahead: int = 8):
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    return await _assignments().find({"expiry_date": {"$gte": now - timedelta(days=1), "$lte": end}, "plan_id": {"$ne": "free"}}).to_list(length=None)


async def reminder_was_sent(owner_id: int, key: str):
    a = await get_assignment(owner_id) or {}
    return key in (a.get("reminders_sent") or [])


async def claim_reminder(
    owner_id: int,
    key: str,
    *,
    stale_after_seconds: int = 600,
) -> bool:
    """
    Atomically reserve one reminder delivery.

    A claim is automatically recoverable after stale_after_seconds. This
    prevents a process crash between claiming and sending from blocking that
    reminder forever.
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(60, int(stale_after_seconds)))

    result = await _assignments().update_one(
        {
            "owner_id": int(owner_id),
            "reminders_sent": {"$ne": key},
            "$or": [
                {"reminder_claims": {"$ne": key}},
                {"reminder_claim_updated_at": {"$lt": stale_before}},
                {"reminder_claim_updated_at": {"$exists": False}},
            ],
        },
        {
            "$addToSet": {"reminder_claims": key},
            "$set": {"reminder_claim_updated_at": now},
        },
    )
    return result.modified_count == 1


async def release_reminder_claim(owner_id: int, key: str):
    await _assignments().update_one(
        {"owner_id": int(owner_id)},
        {
            "$pull": {"reminder_claims": key},
            "$set": {"reminder_claim_updated_at": datetime.now(timezone.utc)},
        },
    )


async def mark_reminder_sent(owner_id: int, key: str):
    """
    Finalize a successfully delivered reminder and release its claim.
    """
    await _assignments().update_one(
        {"owner_id": int(owner_id)},
        {
            "$addToSet": {"reminders_sent": key},
            "$pull": {"reminder_claims": key},
            "$set": {"reminder_sent_at": datetime.now(timezone.utc)},
        },
    )
