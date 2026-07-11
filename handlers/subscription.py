from datetime import timezone
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.subscriptions import get_subscription

IST = ZoneInfo("Asia/Kolkata")


def format_expiry(dt):
    if not dt:
        return "-"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")


async def show_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    subscription = await get_subscription(query.from_user.id)

    if not subscription:
        text = (
            "💎 My Subscription\n\n"
            "❌ You do not have an active subscription."
        )
    else:
        status = "✅ Active" if subscription.get("active") else "❌ Expired"
        text = (
            "💎 My Subscription\n\n"
            f"📦 Plan: {subscription.get('plan', 'No Plan')}\n"
            f"📌 Status: {status}\n"
            f"📅 Expiry: {format_expiry(subscription.get('expiry_date'))}"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Renew / View Plans", callback_data="plans")],
        [InlineKeyboardButton("⬅ Back", callback_data="start")],
    ])

    await query.edit_message_text(text, reply_markup=keyboard)


def subscription_callback():
    return CallbackQueryHandler(
        show_subscription,
        pattern=r"^(subscription|my_subscription)$",
    )
