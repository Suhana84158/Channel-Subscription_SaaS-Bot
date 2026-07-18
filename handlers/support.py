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
from database.live_support import (
    get_private_message_link,
    save_private_message_link,
)

logger = logging.getLogger(__name__)

WAIT_SUPPORT = 1
# Fast in-memory cache. The database mapping remains the source of truth
# after restarts and avoids message-ID collisions between admin chats.
SUPPORT_REPLY_MAP = {}
MAIN_SUPPORT_OWNER_ID = 0


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

            cache_key = (int(admin_id), int(admin_msg.message_id))
            SUPPORT_REPLY_MAP[cache_key] = int(user.id)
            await save_private_message_link(
                owner_id=MAIN_SUPPORT_OWNER_ID,
                admin_chat_id=int(admin_id),
                admin_message_id=int(admin_msg.message_id),
                user_id=int(user.id),
            )
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

    admin_chat_id = int(update.effective_chat.id)
    replied_id = int(message.reply_to_message.message_id)
    cache_key = (admin_chat_id, replied_id)
    user_id = SUPPORT_REPLY_MAP.get(cache_key)

    if not user_id:
        try:
            link = await get_private_message_link(
                owner_id=MAIN_SUPPORT_OWNER_ID,
                admin_chat_id=admin_chat_id,
                admin_message_id=replied_id,
            )
            if link:
                user_id = int(link["user_id"])
                SUPPORT_REPLY_MAP[cache_key] = user_id
        except Exception:
            logger.exception(
                "Failed to resolve persisted support mapping "
                "admin_chat_id=%s replied_message_id=%s",
                admin_chat_id,
                replied_id,
            )

    if not user_id:
        await message.reply_text(
            "❌ This support request mapping is unavailable or expired."
        )
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
