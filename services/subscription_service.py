from database.subscriptions import (
    activate_subscription as db_activate_subscription,
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

    logger.info(f"Subscription activated for {user_id}")
    return expiry


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

    logger.info(f"Subscription renewed for {user_id}")
    return expiry


async def get_user_subscription(user_id: int):
    return await get_subscription(user_id)


async def subscription_active(user_id: int):
    return await is_subscription_active(user_id)
