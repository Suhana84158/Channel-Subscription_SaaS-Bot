from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database.seller_bots import (
    delete_bot,
    get_bot,
    get_bot_by_bot_id,
    save_bot,
    set_bot_active,
)
from database.sellers import get_or_create_seller
from services.bot_manager import bot_manager


def seller_dashboard_keyboard(has_bot: bool, bot_active: bool = False):
    rows = []
    if not has_bot:
        rows.append([InlineKeyboardButton("➕ Connect My Bot", callback_data="seller_add_bot")])
    else:
        rows.extend(
            [
                [InlineKeyboardButton("🤖 My Bot", callback_data="seller_my_bot")],
                [
                    InlineKeyboardButton(
                        "⏸ Pause Bot" if bot_active else "▶️ Resume Bot",
                        callback_data="seller_toggle_bot",
                    )
                ],
                [InlineKeyboardButton("🔄 Replace Token", callback_data="seller_add_bot")],
                [InlineKeyboardButton("🗑 Remove Bot", callback_data="seller_remove_bot_confirm")],
            ]
        )
    rows.append([InlineKeyboardButton("❌ Close", callback_data="seller_close")])
    return InlineKeyboardMarkup(rows)


async def seller_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    seller = await get_or_create_seller(user)
    bot_record = await get_bot(user.id)

    expiry = seller.get("expiry_date") or seller.get("trial_expiry")
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    status = "🟢 Active"
    if seller.get("suspended"):
        status = "🔴 Suspended"
    elif expiry and expiry <= datetime.now(timezone.utc):
        status = "🟠 Trial Expired"

    bot_text = "Not connected"
    if bot_record:
        bot_text = f"@{bot_record.get('bot_username', '-')} ({'Active' if bot_record.get('active') else 'Paused'})"

    text = (
        "🏪 Seller Dashboard\n\n"
        f"👤 Seller: {user.first_name}\n"
        f"📌 Account: {status}\n"
        f"🤖 Bot: {bot_text}\n\n"
        "Phase 1 is active: seller registration, secure token verification, one seller = one bot, pause/resume/remove."
    )

    markup = seller_dashboard_keyboard(bool(bot_record), bool(bot_record and bot_record.get("active")))

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def seller_add_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["waiting_seller_bot_token"] = True
    await query.edit_message_text(
        "🤖 Send your BotFather token.\n\n"
        "The token will be verified with Telegram and stored encrypted.\n"
        "Do not send another person's token.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data="seller_home")]]
        ),
    )


async def receive_seller_bot_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_seller_bot_token"):
        return

    token = update.message.text.strip()
    if ":" not in token or len(token) < 20:
        await update.message.reply_text("❌ Token format is invalid. Send the full BotFather token.")
        return

    status_message = await update.message.reply_text("🔄 Verifying bot token...")

    try:
        candidate = Bot(token=token)
        me = await candidate.get_me()

        existing = await get_bot_by_bot_id(me.id)
        if existing and existing.get("owner_id") != update.effective_user.id:
            await status_message.edit_text("❌ This bot is already registered by another seller.")
            return

        await get_or_create_seller(update.effective_user)
        await save_bot(
            owner_id=update.effective_user.id,
            bot_id=me.id,
            bot_name=me.full_name,
            bot_username=me.username or str(me.id),
            bot_token=token,
        )

        started = await bot_manager.restart_bot(update.effective_user.id)
        context.user_data.pop("waiting_seller_bot_token", None)
        runtime_text = "🟢 Bot is running" if started else "🔴 Bot saved, but runtime failed. Check Render logs."
        await status_message.edit_text(
            "✅ Bot connected successfully!\n\n"
            f"🤖 Name: {me.full_name}\n"
            f"👤 Username: @{me.username}\n"
            f"🆔 Bot ID: {me.id}\n"
            f"{runtime_text}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏪 Seller Dashboard", callback_data="seller_home")]]
            ),
        )

    except InvalidToken:
        await status_message.edit_text("❌ Invalid Bot Token. Check the token and try again.")
    except TelegramError:
        await status_message.edit_text("❌ Telegram could not verify this token. Try again.")
    except RuntimeError as exc:
        await status_message.edit_text(f"❌ Security configuration error: {exc}")
    except Exception:
        await status_message.edit_text("❌ Bot registration failed. Check Render logs.")


async def seller_my_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    record = await get_bot(query.from_user.id)
    if not record:
        await query.edit_message_text(
            "❌ No bot connected.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅ Back", callback_data="seller_home")]]
            ),
        )
        return

    await query.edit_message_text(
        "🤖 My Bot\n\n"
        f"Name: {record.get('bot_name', '-')}\n"
        f"Username: @{record.get('bot_username', '-')}\n"
        f"Bot ID: {record.get('bot_id', '-')}\n"
        f"Status: {'🟢 Active' if record.get('active') else '⏸ Paused'}\n"
        f"Runtime: {record.get('runtime_status', 'unknown')}\n"
        f"Runtime Error: {record.get('runtime_error') or '-'}\n\n"
        "Token is stored encrypted and is never shown here.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data="seller_home")]]
        ),
    )


async def seller_toggle_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    record = await get_bot(query.from_user.id)
    if not record:
        await query.answer("No bot connected.", show_alert=True)
        return

    should_activate = not bool(record.get("active"))
    await set_bot_active(query.from_user.id, should_activate)

    if should_activate:
        await bot_manager.start_bot(query.from_user.id)
    else:
        await bot_manager.stop_bot(query.from_user.id, runtime_status="paused")

    await seller_dashboard(update, context)


async def seller_remove_bot_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚠️ Remove connected bot?\n\nThe encrypted token record will be deleted. Customer data is not deleted in this phase.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, Remove", callback_data="seller_remove_bot")],
                [InlineKeyboardButton("⬅ Cancel", callback_data="seller_home")],
            ]
        ),
    )


async def seller_remove_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await bot_manager.stop_bot(query.from_user.id, runtime_status="removed")
    await delete_bot(query.from_user.id)
    await query.edit_message_text(
        "✅ Bot removed from your seller account.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏪 Seller Dashboard", callback_data="seller_home")]]
        ),
    )


async def seller_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.delete_message()


def seller_handlers():
    return [
        CommandHandler("seller", seller_dashboard),
        CallbackQueryHandler(seller_dashboard, pattern=r"^seller_home$"),
        CallbackQueryHandler(seller_add_bot, pattern=r"^seller_add_bot$"),
        CallbackQueryHandler(seller_my_bot, pattern=r"^seller_my_bot$"),
        CallbackQueryHandler(seller_toggle_bot, pattern=r"^seller_toggle_bot$"),
        CallbackQueryHandler(seller_remove_bot_confirm, pattern=r"^seller_remove_bot_confirm$"),
        CallbackQueryHandler(seller_remove_bot, pattern=r"^seller_remove_bot$"),
        CallbackQueryHandler(seller_close, pattern=r"^seller_close$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_seller_bot_token),
    ]
