from database.subscriptions import (
    activate_subscription as db_activate_subscription,
    fulfill_payment_subscription as db_fulfill_payment_subscription,
    renew_subscription,
    get_subscription,
    is_subscription_active,
)

from logging_config import get_logger

logger = get_logger(__name__)


async def activate_subscription(
    user_id: int,
    plan_days: int = 0,
    plan_name: str = "Premium",
    duration_minutes: int = 0,
):
    expiry = await db_activate_subscription(
        user_id=user_id,
        plan_name=plan_name,
        duration_days=plan_days,
        duration_minutes=duration_minutes,
    )

    logger.info(
        "Subscription activated user_id=%s expiry=%s",
        user_id,
        expiry,
    )
    return expiry


async def fulfill_payment_subscription(
    user_id: int,
    fulfillment_key: str,
    plan_name: str = "Premium",
    plan_days: int = 0,
    duration_minutes: int = 0,
):
    result = await db_fulfill_payment_subscription(
        user_id=user_id,
        fulfillment_key=fulfillment_key,
        plan_name=plan_name,
        duration_days=plan_days,
        duration_minutes=duration_minutes,
    )

    logger.info(
        "Payment subscription fulfillment user_id=%s key=%s "
        "applied=%s action=%s expiry=%s",
        user_id,
        fulfillment_key,
        result["applied"],
        result["action"],
        result["expiry"],
    )
    return result


async def extend_subscription(
    user_id: int,
    plan_days: int = 0,
    duration_minutes: int = 0,
):
    expiry = await renew_subscription(
        user_id=user_id,
        duration_days=plan_days,
        duration_minutes=duration_minutes,
    )

    logger.info(
        "Subscription renewed user_id=%s expiry=%s",
        user_id,
        expiry,
    )
    return expiry


async def get_user_subscription(user_id: int):
    return await get_subscription(user_id)


async def subscription_active(user_id: int):
    return await is_subscription_active(user_id)
