from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database.mongo import get_database

COLLECTION = "coupons"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_code(code: str) -> str:
    return str(code or "").strip().upper()


def coupons_collection():
    return get_database()[COLLECTION]


async def initialize_coupon_indexes() -> None:
    collection = coupons_collection()
    await collection.create_index("code", unique=True)
    await collection.create_index([("active", 1), ("expiry_date", 1)])


async def create_coupon(
    code: str,
    discount: float,
    coupon_type: str = "percent",
    usage_limit: int = 1,
    expiry_date=None,
):
    normalized_code = _normalise_code(code)
    if not normalized_code:
        raise ValueError("Coupon code is required.")

    coupon_type = str(coupon_type or "percent").strip().lower()
    if coupon_type not in {"percent", "fixed"}:
        raise ValueError("coupon_type must be 'percent' or 'fixed'.")

    discount = float(discount)
    usage_limit = int(usage_limit)
    if discount < 0:
        raise ValueError("Discount cannot be negative.")
    if coupon_type == "percent" and discount > 100:
        raise ValueError("Percent discount cannot exceed 100.")
    if usage_limit < 1:
        raise ValueError("usage_limit must be at least 1.")

    now = _utcnow()
    document = {
        "code": normalized_code,
        "discount": discount,
        "coupon_type": coupon_type,
        "usage_limit": usage_limit,
        "used_count": 0,
        "expiry_date": expiry_date,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    try:
        await coupons_collection().insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError("Coupon code already exists.") from exc

    return document


async def get_coupon(code: str):
    normalized_code = _normalise_code(code)
    if not normalized_code:
        return None
    return await coupons_collection().find_one(
        {"code": normalized_code, "active": True}
    )


async def use_coupon(code: str) -> bool:
    """Atomically consume one coupon use without crossing its usage limit."""
    normalized_code = _normalise_code(code)
    if not normalized_code:
        return False

    now = _utcnow()
    coupon = await coupons_collection().find_one_and_update(
        {
            "code": normalized_code,
            "active": True,
            "$expr": {
                "$lt": [
                    {"$ifNull": ["$used_count", 0]},
                    {"$ifNull": ["$usage_limit", 0]},
                ]
            },
            "$or": [
                {"expiry_date": None},
                {"expiry_date": {"$exists": False}},
                {"expiry_date": {"$gt": now}},
            ],
        },
        {
            "$inc": {"used_count": 1},
            "$set": {"updated_at": now},
        },
        return_document=ReturnDocument.AFTER,
    )
    return coupon is not None


async def release_coupon_use(code: str) -> bool:
    """Return one reserved use after a later payment/action fails."""
    normalized_code = _normalise_code(code)
    if not normalized_code:
        return False

    result = await coupons_collection().update_one(
        {
            "code": normalized_code,
            "used_count": {"$gt": 0},
        },
        {
            "$inc": {"used_count": -1},
            "$set": {"updated_at": _utcnow()},
        },
    )
    return result.modified_count == 1


async def disable_coupon(code: str) -> bool:
    normalized_code = _normalise_code(code)
    if not normalized_code:
        return False

    result = await coupons_collection().update_one(
        {"code": normalized_code, "active": True},
        {
            "$set": {
                "active": False,
                "updated_at": _utcnow(),
            }
        },
    )
    return result.modified_count == 1


async def is_coupon_valid(code: str) -> bool:
    coupon = await get_coupon(code)
    if not coupon:
        return False

    expiry_date = coupon.get("expiry_date")
    if expiry_date and expiry_date <= _utcnow():
        return False

    used_count = int(coupon.get("used_count", 0) or 0)
    usage_limit = int(coupon.get("usage_limit", 0) or 0)
    return usage_limit > 0 and used_count < usage_limit


async def calculate_coupon_amount(code: str, amount: float) -> tuple[float | None, str | None]:
    """Validate a coupon and return the discounted amount without consuming it."""
    coupon = await get_coupon(code)
    if not coupon or not await is_coupon_valid(code):
        return None, "Invalid, expired, or fully used coupon."

    amount = max(0.0, float(amount))
    value = max(0.0, float(coupon.get("discount", 0) or 0))
    if coupon.get("coupon_type") == "percent":
        discount = amount * min(value, 100.0) / 100.0
    else:
        discount = value

    return max(0.0, amount - discount), None
