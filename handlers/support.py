import html
import logging

from telegram import ForceReply, Update
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
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
SUPPORT_REPLY_MAP: dict[tuple[int, int], int] = {}
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


def _user_label(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Unknown user"
    name = html.escape(user.full_name or user.first_name or "Unknown")
    username = f"@{html.escape(user.username)}" if user.username else "None"
    return (
        "📞 <b>NEW SUPPORT REQUEST</b>\n\n"
        f"👤 User: {name}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📛 Username: {username}\n\n"
        "Reply to this message to answer the user."
    )


async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📞 <b>Support</b>\n\nSend your problem, photo, video, document or voice message.\n\nAdmin will reply soon.",
        parse_mode="HTML",
    )
    return WAIT_SUPPORT


async def receive_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if not user or not message or not chat:
        return ConversationHandler.END

    receipt = await claim_support_delivery(
        MAIN_SUPPORT_OWNER_ID,
        "user_to_admin",
        chat.id,
        message.message_id,
    )
    if not receipt:
        await message.reply_text("✅ Your support request was already sent.")
        return ConversationHandler.END

    sent = 0
    failed_admins: list[int] = []
    try:
        for admin_id in await _admin_ids():
            try:
                header = await context.bot.send_message(
                    chat_id=admin_id,
                    text=_user_label(update),
                    parse_mode="HTML",
                    reply_markup=ForceReply(selective=True),
                )

                # Keep the mapping on the header because admins reply to it.
                key = (int(admin_id), int(header.message_id))
                SUPPORT_REPLY_MAP[key] = int(user.id)
                await save_private_message_link(
                    owner_id=MAIN_SUPPORT_OWNER_ID,
                    admin_chat_id=admin_id,
                    admin_message_id=header.message_id,
                    user_id=user.id,
                )

                # Copy preserves supported Telegram media without downloading it.
                await context.bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=chat.id,
                    message_id=message.message_id,
                    reply_to_message_id=header.message_id,
                )
                sent += 1
            except (Forbidden, BadRequest) as exc:
                failed_admins.append(int(admin_id))
                logger.warning(
                    "Support delivery rejected admin_id=%s user_id=%s error=%s",
                    admin_id,
                    user.id,
                    exc,
                )
            except (NetworkError, TelegramError) as exc:
                failed_admins.append(int(admin_id))
                logger.warning(
                    "Support delivery failed admin_id=%s user_id=%s error=%s",
                    admin_id,
                    user.id,
                    exc,
                )
            except Exception:
                failed_admins.append(int(admin_id))
                logger.exception(
                    "Unexpected support delivery error admin_id=%s user_id=%s",
                    admin_id,
                    user.id,
                )

        if sent == 0:
            raise RuntimeError("No support admin received the request")

        await complete_support_delivery(
            receipt["_id"],
            user_id=user.id,
            delivered_admin_count=sent,
            failed_admin_ids=failed_admins,
        )
        await message.reply_text("✅ Your support request has been sent.")
    except Exception as exc:
        await fail_support_delivery(receipt["_id"], str(exc))
        await message.reply_text("❌ Failed to send support request. Please try again.")

    return ConversationHandler.END


async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.reply_to_message:
        return

    admin_id = update.effective_user.id if update.effective_user else 0
    if admin_id not in await _admin_ids():
        return

    admin_chat_id = int(chat.id)
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
        label = await context.bot.send_message(
            chat_id=user_id,
            text="📞 <b>Admin Reply</b>",
            parse_mode="HTML",
        )
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=admin_chat_id,
            message_id=message.message_id,
            reply_to_message_id=label.message_id,
        )
        await complete_support_delivery(
            receipt["_id"], user_id=user_id, admin_id=admin_id
        )
        await message.reply_text("✅ Reply sent to user.")
    except Forbidden as exc:
        logger.warning("User blocked bot user_id=%s error=%s", user_id, exc)
        await fail_support_delivery(receipt["_id"], f"user_forbidden: {exc}")
        await message.reply_text("❌ User has blocked the bot or cannot receive messages.")
    except (BadRequest, NetworkError, TelegramError) as exc:
        logger.warning(
            "Failed to deliver support reply admin_id=%s user_id=%s error=%s",
            admin_id,
            user_id,
            exc,
        )
        await fail_support_delivery(receipt["_id"], str(exc))
        await message.reply_text("❌ Failed to send reply. Please try again.")
    except Exception as exc:
        logger.exception(
            "Unexpected support reply error admin_id=%s user_id=%s",
            admin_id,
            user_id,
        )
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
        allow_reentry=True,
    )


def support_reply_handler():
    return MessageHandler(filters.REPLY & ~filters.COMMAND, admin_reply_handler)
