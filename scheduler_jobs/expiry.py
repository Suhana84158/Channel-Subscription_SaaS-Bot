from datetime import datetime, timezone

from database.subscriptions import (
    expire_subscription,
    get_expired_subscriptions,
    get_subscription,
    make_aware,
    subscription_lock,
)
from services.telegram_service import (
    remove_user_from_channels,
    send_expiry_message,
)
from logging_config import get_logger

logger = get_logger(__name__)


async def check_expired_subscriptions():
    """Process expired subscriptions safely without racing renewals."""
    expired_users = await get_expired_subscriptions(
        datetime.now(timezone.utc)
    )

    for snapshot in expired_users:
        user_id = snapshot.get("user_id")
        if user_id is None:
            continue

        try:
            async with subscription_lock(user_id):
                current = await get_subscription(user_id)
                if not current or not current.get("active"):
                    continue

                current_expiry = make_aware(current.get("expiry_date"))
                if not current_expiry or current_expiry > datetime.now(timezone.utc):
                    continue

                await remove_user_from_channels(user_id)
                await send_expiry_message(user_id)

                expired = await expire_subscription(
                    user_id,
                    expected_expiry=current.get("expiry_date"),
                )

                if expired:
                    logger.info(
                        "Expired subscription processed user_id=%s expiry=%s",
                        user_id,
                        current_expiry,
                    )
                else:
                    logger.warning(
                        "Expiry state changed before final update user_id=%s",
                        user_id,
                    )

        except Exception:
            logger.exception(
                "Failed processing expired subscription user_id=%s",
                user_id,
            )
