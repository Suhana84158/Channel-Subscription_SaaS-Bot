import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from database.admins import is_admin
from services.statistics_service import build_platform_statistics_text

logger = logging.getLogger(__name__)


async def statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if not await is_admin(user.id):
        await message.reply_text("❌ You are not authorized.")
        return

    try:
        text = await build_platform_statistics_text()
        await message.reply_text(
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to build platform statistics for admin_id=%s", user.id)
        await message.reply_text(
            "❌ Statistics load nahi ho payi. Thodi der baad dobara try karo."
        )


def statistics_handler():
    return CommandHandler("stats", statistics)
