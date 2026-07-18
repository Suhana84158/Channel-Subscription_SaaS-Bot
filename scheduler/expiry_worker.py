from datetime import datetime, timezone

from database.subscriptions import (
    claim_expired_subscription,
    claim_expiry_notification,
    complete_expiry_claim,
    complete_expiry_notification,
    get_expired_subscriptions,
    release_expiry_claim,
    release_expiry_notification,
)
from services.channel_service import (
    revoke_channel_access,
    send_expiry_notification,
)
from logging_config import get_logger

logger = get_logger(__name__)


async def check_expired_users():
    now = datetime.now(timezone.utc)
    subscriptions = await get_expired_subscriptions(now)

    for snapshot in subscriptions:
        user_id = snapshot.get("user_id")
        if user_id is None:
            continue

        claimed = await claim_expired_subscription(
            user_id,
            now=datetime.now(timezone.utc),
            stale_after_seconds=900,
        )
        if not claimed:
            continue

        claim_token = claimed["_claim_token"]
        expected_expiry = claimed.get("expiry_date")

        try:
            result = await revoke_channel_access(
                user_id,
                send_notification=False,
            )

            # Do not mark the subscription expired while any configured
            # channel removal failed. The claim is released for a later retry.
            if result["failed_chat_ids"]:
                await release_expiry_claim(
                    user_id,
                    claim_token,
                    error=(
                        "Failed channel removals: "
                        + ",".join(
                            str(chat_id)
                            for chat_id in result["failed_chat_ids"]
                        )
                    ),
                )
                logger.warning(
                    "Expiry retry scheduled user_id=%s failed_channels=%s",
                    user_id,
                    result["failed_chat_ids"],
                )
                continue

            completed = await complete_expiry_claim(
                user_id,
                claim_token,
                expected_expiry,
            )
            if not completed:
                logger.info(
                    "Expiry finalization skipped because subscription "
                    "changed user_id=%s",
                    user_id,
                )
                continue

            notification_token = await claim_expiry_notification(user_id)
            if notification_token:
                sent = await send_expiry_notification(
                    user_id,
                    removed=result["removed"],
                )
                if sent:
                    await complete_expiry_notification(
                        user_id,
                        notification_token,
                    )
                else:
                    await release_expiry_notification(
                        user_id,
                        notification_token,
                        error="Telegram notification failed",
                    )

            logger.info(
                "Expired subscription processed user_id=%s expiry=%s "
                "removed=%s",
                user_id,
                expected_expiry,
                result["removed"],
            )

        except Exception as exc:
            logger.exception(
                "Failed processing expired subscription user_id=%s",
                user_id,
            )
            try:
                await release_expiry_claim(
                    user_id,
                    claim_token,
                    error=str(exc),
                )
            except Exception:
                logger.exception(
                    "Failed releasing expiry claim user_id=%s",
                    user_id,
                )
