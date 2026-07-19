from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from config import BOT_TOKEN
from database.channels import get_all_channels
from database.payments import mark_payment_channel_delivered
from database.subscription_guard import get_active_invite, save_invite
from logging_config import get_logger

logger = get_logger(__name__)

bot = Bot(token=BOT_TOKEN)


async def grant_channel_access(
    user_id: int,
    *,
    payment_id=None,
    already_delivered_chat_ids=None,
    owner_id: int = 0,
):
    """
    Grant access and return structured delivery results.

    When payment_id is supplied, each successful channel delivery is recorded
    immediately. A retry skips channels already delivered for that payment.
    """
    channels = await get_all_channels()
    delivered = []
    failed = []

    skipped = {
        int(chat_id)
        for chat_id in (already_delivered_chat_ids or [])
    }

    for channel in channels:
        chat_id = int(channel["chat_id"])

        if chat_id in skipped:
            continue

        try:
            invite_doc = await get_active_invite(owner_id, user_id, chat_id)
            invite_link = (invite_doc or {}).get("invite_link")

            if not invite_link:
                invite = await bot.create_chat_invite_link(
                    chat_id=chat_id,
                    member_limit=1,
                )
                invite_link = invite.invite_link
                await save_invite(owner_id, user_id, chat_id, invite_link)

            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🎉 Access Granted\n\n"
                    f"📢 {channel.get('title', 'Premium Channel')}\n\n"
                    f"{invite_link}"
                ),
            )

            delivered.append(chat_id)

            if payment_id is not None:
                recorded = await mark_payment_channel_delivered(
                    payment_id,
                    chat_id,
                )
                if not recorded:
                    raise RuntimeError(
                        "Payment delivery progress could not be recorded."
                    )

        except Exception as exc:
            failed.append({
                "chat_id": chat_id,
                "error": str(exc),
            })
            logger.exception(
                "Failed granting access user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )

    return {
        "delivered_chat_ids": delivered,
        "failed": failed,
        "total_channels": len(channels),
    }


async def revoke_channel_access(
    user_id: int,
    *,
    send_notification: bool = True,
):
    """
    Remove a user from every configured channel/group.

    Telegram ban+unban is safe to retry. The structured result allows the
    expiry worker to decide whether database finalization should continue.
    """
    channels = await get_all_channels()
    removed = 0
    failed = []

    for channel in channels:
        chat_id = channel.get("chat_id")
        try:
            await bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
            )
            await bot.unban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                only_if_banned=True,
            )
            removed += 1

        except TelegramError as exc:
            failed.append(chat_id)
            logger.exception(
                "Failed removing user_id=%s from chat_id=%s: %s",
                user_id,
                chat_id,
                exc,
            )

    if send_notification:
        await send_expiry_notification(user_id, removed=removed)

    logger.info(
        "Expired user_id=%s removed=%s failed=%s",
        user_id,
        removed,
        len(failed),
    )
    return {
        "removed": removed,
        "failed_chat_ids": failed,
        "total_channels": len(channels),
    }


async def send_expiry_notification(
    user_id: int,
    *,
    removed: int = 0,
):
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⏰ Your subscription has expired.\n\n"
                "Access to premium channel/group has been removed.\n"
                "Use 🔄 Renew Plan to continue."
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Renew Plan",
                        callback_data="plans",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "👤 My Profile",
                        callback_data="profile",
                    )
                ],
            ]),
        )
        return True
    except TelegramError as exc:
        logger.warning(
            "Could not send expiry notification user_id=%s removed=%s: %s",
            user_id,
            removed,
            exc,
        )
        return False
