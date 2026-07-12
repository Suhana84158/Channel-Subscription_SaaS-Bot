from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.subscriptions import get_subscription


async def subscription_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    subscription = await get_subscription(query.from_user.id)

    if not subscription:
        await query.edit_message_text(
            "❌ You don't have any active subscription.\n\n"
            "Please buy a plan first.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 View Plans", callback_data="plans")]
            ]),
        )
        return

    status = "✅ Active" if subscription.get("active") else "❌ Expired"

    text = (
        "💎 *My Subscription*\n\n"
        f"📦 Plan: {subscription.get('plan', 'N/A')}\n"
        f"📅 Expiry: {subscription.get('expiry_date', 'N/A')}\n"
        f"📌 Status: {status}"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 Renew Subscription", callback_data="plans")],
        [InlineKeyboardButton("⬅ Back", callback_data="start")],
    ]

    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


def subscription_callback():
    return CallbackQueryHandler(
        subscription_handler,
        pattern="^subscription$|^renew$",
    )
