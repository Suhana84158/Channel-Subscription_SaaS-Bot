from datetime import datetime, timedelta, timezone
from database.mongo import get_database

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


async def initialize_seller_subscription_indexes():
    await _settings().create_index("key", unique=True)
    await _assignments().create_index("owner_id", unique=True)
    await initialize_defaults()


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


async def get_config():
    await initialize_defaults()
    return await _settings().find_one({"key": "config"}) or {}


async def update_config(**values):
    values["updated_at"] = datetime.now(timezone.utc)
    await _settings().update_one({"key": "config"}, {"$set": values}, upsert=True)
    return await get_config()


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
    config = await get_config()
    if plan_id == "free":
        expiry = None
    else:
        plan = await get_paid_plan(plan_id)
        if not plan:
            raise ValueError("Paid plan not found")
        duration = int(days or plan.get("duration_days", 30))
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
        f"• {_limit_text(plan.get('bot_limit', 1))} Child Bot"
        f"{'s' if int(plan.get('bot_limit', 1) or 0) != 1 else ''}\n"
        f"• {_limit_text(plan.get('active_subscriber_limit', 25))} Active Subscribers\n"
        f"• {_limit_text(plan.get('channel_limit', 1))} Channel/Group"
        f"{'s' if int(plan.get('channel_limit', 1) or 0) != 1 else ''}\n"
        f"• {_limit_text(plan.get('plan_limit', 2))} Subscription Plans\n\n"
        "Upgrade your seller plan to continue."
    )


async def seller_usage(owner_id: int):
    from database.seller_bots import get_bot
    from database.seller_data import active_subscriptions, get_channels, get_plans
    bot_count = 1 if await get_bot(owner_id) else 0
    return {
        "bot_count": bot_count,
        "active_subscriber_count": len(await active_subscriptions(owner_id)),
        "channel_count": len(await get_channels(owner_id)),
        "plan_count": len(await get_plans(owner_id)),
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
        f"{row('🤖 Child Bots', usage['bot_count'], 'bot_limit')}\n"
        f"{row('👥 Active Subscribers', usage['active_subscriber_count'], 'active_subscriber_limit')}\n"
        f"{row('📢 Channels/Groups', usage['channel_count'], 'channel_limit')}\n"
        f"{row('📦 Subscription Plans', usage['plan_count'], 'plan_limit')}\n\n"
        f"Status: {status}"
    )
