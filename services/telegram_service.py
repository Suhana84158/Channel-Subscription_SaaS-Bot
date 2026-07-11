from telegram import Bot

from config import BOT_TOKEN
from database.channels import get_all_channels

bot = Bot(BOT_TOKEN)


async def remove_user_from_channels(user_id: int):
    """
    Remove user from all premium channels.
    """

    channels = await get_all_channels()

    for channel in channels:
        try:
            await bot.ban_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id,
            )

            await bot.unban_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id,
            )

        except Exception:
            pass


async def send_expiry_message(user_id: int):
    """
    Notify user that subscription expired.
    """

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "❌ Your subscription has expired.\n\n"
                "Please renew to continue using premium channels."
            ),
        )

    except Exception:
        pass
