from datetime import datetime, timezone

from database.subscriptions import (
    get_expired_subscriptions,
    expire_subscription,
)

from services.telegram_service import (
    remove_user_from_channels,
    send_expiry_message,
)

from logging_config import get_logger

logger = get_logger(__name__)


async def check_expired_subscriptions():
    """
    Check all expired subscriptions
    and remove users automatically.
    """

    now = datetime.now(timezone.utc)

    expired_users = await get_expired_subscriptions(now)

    if not expired_users:
        return

    for subscription in expired_users:
        try:
            user_id = subscription["user_id"]

            await remove_user_from_channels(user_id)

            await send_expiry_message(user_id)

            await expire_subscription(user_id)

            logger.info(
                f"Expired subscription processed: {user_id}"
            )

        except Exception as e:
            logger.exception(e)
