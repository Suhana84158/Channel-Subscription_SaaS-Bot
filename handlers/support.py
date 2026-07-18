import logging

from telegram import Update, ForceReply
from telegram.ext import (
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from config import ADMIN_IDS
from database.admins import get_all_admins

logger = logging.getLogger(__name__)

WAIT_SUPPORT = 1
SUPPORT_REPLY_MAP = {}


async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📞 *Support*\n\n"
        "Please send your problem.\n\n"
        "Admin will reply soon.",
        parse_mode="Markdown",
    )

    return WAIT_SUPPORT


async def receive_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    admin_ids = set(ADMIN_IDS)

    try:
        admins = await get_all_admins()
        for admin in admins:
            admin_id = admin.get("admin_id") or admin.get("user_id")
            if admin_id:
                admin_ids.add(int(admin_id))
    except Exception:
        logger.exception("Failed to load additional support admins")

    text = (
        "📞 NEW SUPPORT REQUEST\n\n"
        f"👤 User: {user.first_name}\n"
        f"🆔 User ID: {user.id}\n"
        f"📛 Username: @{user.username if user.username else 'None'}\n\n"
        f"💬 Message:\n{message.text}"
    )

    sent = 0

    for admin_id in admin_ids:
        try:
            admin_msg = await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=ForceReply(selective=True),
            )

            SUPPORT_REPLY_MAP[admin_msg.message_id] = user.id
            sent += 1

        except Exception:
            logger.exception(
                "Failed to forward support request admin_id=%s user_id=%s",
                admin_id,
                user.id,
            )

    if sent:
        await message.reply_text("✅ Your support request has been sent.")
    else:
        await message.reply_text("❌ Failed to send support request.")

    return ConversationHandler.END


async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message.reply_to_message:
        return

    replied_id = message.reply_to_message.message_id
    user_id = SUPPORT_REPLY_MAP.get(replied_id)

    if not user_id:
        return

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "📞 *Admin Reply*\n\n"
                f"{message.text}"
            ),
            parse_mode="Markdown",
        )

        await message.reply_text("✅ Reply sent to user.")

    except Exception:
        logger.exception(
            "Failed to deliver support reply admin_id=%s user_id=%s",
            update.effective_user.id if update.effective_user else None,
            user_id,
        )
        await message.reply_text("❌ Failed to send reply. Please try again.")


def support_callback():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                support_handler,
                pattern="^support$",
            )
        ],
        states={
            WAIT_SUPPORT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_support_message,
                )
            ]
        },
        fallbacks=[],
    )


def support_reply_handler():
    return MessageHandler(
        filters.TEXT & filters.REPLY,
        admin_reply_handler,
    )
