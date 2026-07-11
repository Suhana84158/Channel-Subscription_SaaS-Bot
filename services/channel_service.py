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

        except TelegramError as e:
            logger.exception(
                f"Failed to create invite for {channel.get('chat_id')}: {e}"
            )


async def revoke_channel_access(user_id: int):
    channels = await get_all_channels()
    removed = 0

    for channel in channels:
        try:
            await bot.ban_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id,
            )

            await bot.unban_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id,
                only_if_banned=True,
            )

            removed += 1

        except TelegramError as e:
            logger.exception(
                f"Failed removing {user_id} from {channel.get('chat_id')}: {e}"
            )

    try:
        if removed > 0:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "⏰ Your subscription has expired.\n\n"
                    "Access to premium channel/group has been removed.\n"
                    "Use 🔄 Renew Plan to continue."
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Renew Plan", callback_data="plans")],
                    [InlineKeyboardButton("👤 My Profile", callback_data="profile")],
                ]),
            )
    except TelegramError:
        pass

    logger.info(f"Expired user {user_id} removed from {removed} channels.")
