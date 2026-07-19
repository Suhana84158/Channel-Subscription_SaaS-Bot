"""Consistent, read-only platform statistics queries."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from database.channels import channels_collection
from database.payments import payments_collection
from database.subscriptions import subscriptions_collection
from database.users import users_collection


APPROVED_PAYMENT_STATUSES = ("approved",)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _sum_approved_revenue(*, since: datetime | None = None) -> float:
    match: dict[str, Any] = {"status": {"$in": list(APPROVED_PAYMENT_STATUSES)}}
    if since is not None:
        # Older records do not always have processed_at, so use the best
        # available timestamp without changing the stored documents.
        match["$expr"] = {
            "$gte": [
                {
                    "$ifNull": [
                        "$processed_at",
                        {"$ifNull": ["$updated_at", "$created_at"]},
                    ]
                },
                since,
            ]
        }

    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": None,
                "total": {
                    "$sum": {
                        "$convert": {
                            "input": "$amount",
                            "to": "double",
                            "onError": 0,
                            "onNull": 0,
                        }
                    }
                },
            }
        },
    ]
    rows = await payments_collection().aggregate(pipeline).to_list(length=1)
    return float(rows[0].get("total", 0) or 0) if rows else 0.0


async def get_platform_statistics() -> dict[str, Any]:
    """Return one internally consistent snapshot for the owner dashboard.

    Counts use the same ``now`` value, so a subscription cannot be shown as
    active in one field and expired in another field during the same request.
    """

    now = _utc_now()
    day_start = now - timedelta(days=1)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    active_query = {"active": True, "expiry_date": {"$gt": now}}
    expired_query = {
        "$or": [
            {"active": False},
            {"expiry_date": {"$lte": now}},
        ]
    }

    (
        users,
        banned_users,
        channels,
        total_subscriptions,
        active_subscriptions,
        expired_subscriptions,
        pending_payments,
        processing_payments,
        approved_payments,
        rejected_payments,
        expired_payments,
        revenue,
        revenue_24h,
        revenue_7d,
        revenue_30d,
    ) = await asyncio.gather(
        users_collection().count_documents({}),
        users_collection().count_documents({"banned": True}),
        channels_collection().count_documents({}),
        subscriptions_collection().count_documents({}),
        subscriptions_collection().count_documents(active_query),
        subscriptions_collection().count_documents(expired_query),
        payments_collection().count_documents({"status": "pending"}),
        payments_collection().count_documents({"status": "processing"}),
        payments_collection().count_documents(
            {"status": {"$in": list(APPROVED_PAYMENT_STATUSES)}}
        ),
        payments_collection().count_documents({"status": "rejected"}),
        payments_collection().count_documents({"status": "expired"}),
        _sum_approved_revenue(),
        _sum_approved_revenue(since=day_start),
        _sum_approved_revenue(since=week_start),
        _sum_approved_revenue(since=month_start),
    )

    active_users_pipeline = [
        {"$match": active_query},
        {"$group": {"_id": "$user_id"}},
        {"$count": "total"},
    ]
    active_user_rows = await subscriptions_collection().aggregate(
        active_users_pipeline
    ).to_list(length=1)
    active_users = int(active_user_rows[0]["total"]) if active_user_rows else 0

    return {
        "users": int(users),
        "active_users": active_users,
        "banned_users": int(banned_users),
        "channels": int(channels),
        "total_subscriptions": int(total_subscriptions),
        "active_subscriptions": int(active_subscriptions),
        "expired_subscriptions": int(expired_subscriptions),
        "pending_payments": int(pending_payments),
        "processing_payments": int(processing_payments),
        "approved_payments": int(approved_payments),
        "rejected_payments": int(rejected_payments),
        "expired_payments": int(expired_payments),
        "revenue": float(revenue),
        "revenue_24h": float(revenue_24h),
        "revenue_7d": float(revenue_7d),
        "revenue_30d": float(revenue_30d),
        "generated_at": now,
    }
