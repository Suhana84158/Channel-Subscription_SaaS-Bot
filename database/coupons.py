from datetime import datetime, timezone

from database.mongo import get_database

COLLECTION = "coupons"


def coupons_collection():
    return get_database()[COLLECTION]


async def create_coupon(
    code: str,
    discount: float,
    coupon_type: str = "percent",
    usage_limit: int = 1,
    expiry_date=None,
):
    document = {
        "code": code.upper(),
        "discount": discount,
        "coupon_type": coupon_type,
        "usage_limit": usage_limit,
        "used_count": 0,
        "expiry_date": expiry_date,
        "active": True,
        "created_at": datetime.now(timezone.utc),
    }

    await coupons_collection().insert_one(document)

    return document


async def get_coupon(code: str):
    return await coupons_collection().find_one(
        {
            "code": code.upper(),
            "active": True,
        }
    )


async def use_coupon(code: str):
    await coupons_collection().update_one(
        {"code": code.upper()},
        {
            "$inc": {"used_count": 1}
        },
    )


async def disable_coupon(code: str):
    await coupons_collection().update_one(
        {"code": code.upper()},
        {
            "$set": {"active": False}
        },
    )


async def is_coupon_valid(code: str):
    coupon = await get_coupon(code)

    if not coupon:
        return False

    if (
        coupon["expiry_date"]
        and coupon["expiry_date"] < datetime.now(timezone.utc)
    ):
        return False

    if coupon["used_count"] >= coupon["usage_limit"]:
        return False

    return True
