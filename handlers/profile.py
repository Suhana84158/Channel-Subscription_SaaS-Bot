from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes
from zoneinfo import ZoneInfo

from database.users import get_user
from database.subscriptions import get_subscription


def format_ist(dt):
    if not dt:
        return "-"

    return dt.astimezone(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%d-%m-%Y %I:%M:%S %p IST")


async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = await get_user(query.from_user.id)

    if not user:
        await query.edit_message_text(
            "❌ Profile not found.\nPlease send /start first."
        )
        return

    subscription = await get_subscription(query.from_user.id)

    if subscription:
        plan = subscription.get("plan", "No Plan")
        expiry = format_ist(subscription.get("expiry_date"))
        status = "✅ Active" if subscription.get("active") else "❌ Expired"
    else:
        plan = "No Plan"
        expiry = "-"
        status = "Inactive"

    username = user.get("username")
    if username:
        username = "@" + username
    else:
        username = "None"

    text = (
        "👤 My Profile\n\n"
        f"🆔 ID: {user.get('user_id')}\n"
        f"👤 Name: {user.get('first_name')}\n"
        f"📛 Username: {username}\n\n"
        f"💎 Plan: {plan}\n"
        f"📅 Expiry: {expiry}\n"
        f"📌 Status: {status}"
    )

    await query.edit_message_text(text)


def profile_callback():
    return CallbackQueryHandler(
        profile_handler,
        pattern="^profile$",
    )
