import logging

from telegram import ForceReply, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_IDS
from database.admins import get_all_admins
from database.live_support import (
    claim_support_delivery,
    complete_support_delivery,
    fail_support_delivery,
    get_private_message_link,
    save_private_message_link,
)

logger = logging.getLogger(__name__)

WAIT_SUPPORT = 1
SUPPORT_REPLY_MAP = {}
MAIN_SUPPORT_OWNER_ID = 0


async def _admin_ids() -> set[int]:
    ids = {int(value) for value in ADMIN_IDS}
    try:
        for admin in await get_all_admins():
            value = admin.get("admin_id") or admin.get("user_id")
            if value:
                ids.add(int(value))
    except Exception:
        logger.exception("Failed to load additional support admins")
    return ids


async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📞 *Support*\n\nPlease send your problem.\n\nAdmin will reply soon.",
        parse_mode="Markdown",
    )
    return WAIT_SUPPORT


async def receive_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return ConversationHandler.END

    receipt = await claim_support_delivery(
        MAIN_SUPPORT_OWNER_ID,
        "user_to_admin",
        update.effective_chat.id,
        message.message_id,
    )
    if not receipt:
        await message.reply_text("✅ Your support request was already sent.")
        return ConversationHandler.END

    body = message.text or message.caption or "[Media message]"
    text = (
        "📞 NEW SUPPORT REQUEST\n\n"
        f"👤 User: {user.first_name}\n"
        f"🆔 User ID: {user.id}\n"
        f"📛 Username: @{user.username if user.username else 'None'}\n\n"
        f"💬 Message:\n{body}"
    )

    sent = 0
    try:
        for admin_id in await _admin_ids():
            try:
                admin_msg = await context.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=ForceReply(selective=True),
                )
                # Preserve photos, documents, video, voice and other Telegram media.
                if not message.text:
                    await context.bot.copy_message(
                        chat_id=admin_id,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id,
                    )
                key = (int(admin_id), int(admin_msg.message_id))
                SUPPORT_REPLY_MAP[key] = int(user.id)
                await save_private_message_link(
                    owner_id=MAIN_SUPPORT_OWNER_ID,
                    admin_chat_id=admin_id,
                    admin_message_id=admin_msg.message_id,
                    user_id=user.id,
                )
                sent += 1
            except Exception:
                logger.exception(
                    "Failed to forward support request admin_id=%s user_id=%s",
                    admin_id,
                    user.id,
                )

        if sent == 0:
            raise RuntimeError("No support admin received the request")

        await complete_support_delivery(
            receipt["_id"], user_id=user.id, delivered_admin_count=sent
        )
        await message.reply_text("✅ Your support request has been sent.")
    except Exception as exc:
        await fail_support_delivery(receipt["_id"], str(exc))
        await message.reply_text("❌ Failed to send support request. Please try again.")

    return ConversationHandler.END


async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.reply_to_message:
        return

    admin_id = update.effective_user.id if update.effective_user else 0
    if admin_id not in await _admin_ids():
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
            logger.exception("Failed to resolve persisted support mapping")

    if not user_id:
        await message.reply_text("❌ This support request mapping is unavailable or expired.")
        return

    receipt = await claim_support_delivery(
        MAIN_SUPPORT_OWNER_ID,
        "admin_to_user",
        admin_chat_id,
        message.message_id,
    )
    if not receipt:
        await message.reply_text("✅ This reply was already sent.")
        return

    try:
        if message.text:
            await context.bot.send_message(
                chat_id=user_id,
                text="📞 Admin Reply\n\n" + message.text,
            )
        else:
            await context.bot.send_message(chat_id=user_id, text="📞 Admin Reply")
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
        await complete_support_delivery(receipt["_id"], user_id=user_id, admin_id=admin_id)
        await message.reply_text("✅ Reply sent to user.")
    except Exception as exc:
        logger.exception("Failed to deliver support reply admin_id=%s user_id=%s", admin_id, user_id)
        await fail_support_delivery(receipt["_id"], str(exc))
        await message.reply_text("❌ Failed to send reply. Please try again.")


def support_callback():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(support_handler, pattern="^support$")],
        states={
            WAIT_SUPPORT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, receive_support_message)
            ]
        },
        fallbacks=[],
    )


def support_reply_handler():
    return MessageHandler(filters.REPLY & ~filters.COMMAND, admin_reply_handler)
