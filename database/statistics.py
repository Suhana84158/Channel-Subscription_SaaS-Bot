from datetime import datetime, timezone

from database.channels import channels_collection
from database.payments import payments_collection
from database.subscriptions import subscriptions_collection
from database.users import users_collection


async def get_platform_statistics() -> dict:
    now = datetime.now(timezone.utc)
    revenue_pipeline = [
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$amount", 0]}}}},
    ]
    revenue_rows = await payments_collection().aggregate(revenue_pipeline).to_list(length=1)

    users = await users_collection().count_documents({})
    banned_users = await users_collection().count_documents({"banned": True})
    channels = await channels_collection().count_documents({})
    active_subscriptions = await subscriptions_collection().count_documents(
        {"active": True, "expiry_date": {"$gt": now}}
    )
    pending_payments = await payments_collection().count_documents({"status": "pending"})
    approved_payments = await payments_collection().count_documents({"status": "approved"})
    rejected_payments = await payments_collection().count_documents({"status": "rejected"})

    return {
        "users": users,
        "banned_users": banned_users,
        "channels": channels,
        "active_subscriptions": active_subscriptions,
        "pending_payments": pending_payments,
        "approved_payments": approved_payments,
        "rejected_payments": rejected_payments,
        "revenue": revenue_rows[0]["total"] if revenue_rows else 0,
        "generated_at": now,
    }
