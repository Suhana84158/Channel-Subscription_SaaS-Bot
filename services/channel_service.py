from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from config import BOT_TOKEN
from database.channels import get_all_channels
from logging_config import get_logger

logger = get_logger(__name__)

bot = Bot(token=BOT_TOKEN)


async def grant_channel_access(user_id: int):
    channels = await get_all_channels()

    for channel in channels:
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=channel["chat_id"],
                member_limit=1,
            )

            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🎉 Access Granted\n\n"
                    f"📢 {channel.get('title', 'Premium Channel')}\n\n"
                    f"{invite.invite_link}"
                ),
            )

        except TelegramError as exc:
            logger.exception(
                "Failed to create invite for %s: %s",
                channel.get("chat_id"),
                exc,
            )


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
