import logging
from datetime import datetime, timezone

from config import ADMIN_IDS
from database.seller_subscriptions import (
    claim_reminder,
    expiring_assignments,
    mark_reminder_sent,
    release_reminder_claim,
    usage_warning,
)

logger = logging.getLogger(__name__)


async def _send_admin_copies(bot, seller_id: int, text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"Seller {seller_id}: {text}",
            )
        except Exception:
            logger.exception(
                "Seller subscription reminder admin copy failed "
                "seller_id=%s admin_id=%s",
                seller_id,
                admin_id,
            )


async def run_seller_subscription_reminders(bot):
    """
    Send each expiry reminder once.

    The reminder is marked as sent only after the seller actually receives it.
    Atomic claims also prevent duplicate messages when multiple scheduler
    instances overlap.
    """
    now = datetime.now(timezone.utc)

    for assignment in await expiring_assignments(8):
        owner_id = int(assignment["owner_id"])
        expiry = assignment.get("expiry_date")

        if not expiry:
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        seconds = (expiry - now).total_seconds()
        days = max(0, int((seconds + 86399) // 86400))

        if seconds <= 0:
            label = "expired"
        elif days in {7, 3, 1}:
            label = f"{days}d"
        else:
            continue

        key = f"{expiry.isoformat()}:{label}"

        try:
            claimed = await claim_reminder(
                owner_id,
                key,
                stale_after_seconds=600,
            )
        except Exception:
            logger.exception(
                "Seller subscription reminder claim failed "
                "seller_id=%s key=%s",
                owner_id,
                key,
            )
            continue

        if not claimed:
            continue

        if label == "expired":
            text = (
                "⛔ Your seller plan has expired. Existing subscribers keep "
                "access, but new payments, users, plans and channels are "
                "restricted until renewal."
            )
        else:
            text = (
                "⏰ Seller Plan Expiry Reminder\n\n"
                f"Your plan expires in {days} "
                f"day{'s' if days != 1 else ''}. Renew before expiry to "
                "avoid restrictions."
            )

        try:
            await bot.send_message(owner_id, text)
        except Exception:
            logger.exception(
                "Seller subscription reminder delivery failed "
                "seller_id=%s key=%s",
                owner_id,
                key,
            )
            try:
                await release_reminder_claim(owner_id, key)
            except Exception:
                logger.exception(
                    "Seller subscription reminder claim release failed "
                    "seller_id=%s key=%s",
                    owner_id,
                    key,
                )
            continue

        try:
            await mark_reminder_sent(owner_id, key)
        except Exception:
            logger.exception(
                "Seller subscription reminder finalize failed "
                "seller_id=%s key=%s",
                owner_id,
                key,
            )
            # Keep the claim temporarily when finalization fails. The stale-claim
            # timeout releases it automatically after 10 minutes so a transient
            # database failure cannot block the reminder forever.
            continue

        await _send_admin_copies(bot, owner_id, text)

        try:
            warning = await usage_warning(owner_id, 0.8)
            if warning:
                await bot.send_message(owner_id, warning)
        except Exception:
            logger.exception(
                "Seller plan usage warning failed seller_id=%s",
                owner_id,
            )
