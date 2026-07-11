from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.channels import get_all_channels

PLAN_CACHE = {}


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    channels = await get_all_channels()

    text = "📋 Available Subscription Plans\n\n"
    keyboard = []

    PLAN_CACHE.clear()

    count = 0

    for channel in channels:
        title = channel.get("title", "Premium Channel")
        plans = channel.get("plans", [])

        if not plans:
            continue

        text += f"📢 {title}\n"

        for plan in plans:
            count += 1
            plan_key = f"p{count}"

            PLAN_CACHE[plan_key] = {
                "name": f"{title} - {plan['duration_text']}",
                "price": plan["price"],
                "days": plan.get("duration_days", 1),
                "duration_minutes": plan["duration_minutes"],
                "duration_text": plan["duration_text"],
                "channel_id": channel["chat_id"],
            }

            text += f"⭐ {plan['duration_text']} - ₹{plan['price']}\n"

            keyboard.append([
                InlineKeyboardButton(
                    f"Buy {plan['duration_text']} ₹{plan['price']}",
                    callback_data=f"buy_{plan_key}",
                )
            ])

        text += "\n"

    if count == 0:
        text += "❌ No plans available."

    keyboard.append([
        InlineKeyboardButton("⬅ Back", callback_data="start")
    ])

    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def get_plan(plan_name: str):
    return PLAN_CACHE.get(plan_name)


def plans_handler():
    return CallbackQueryHandler(
        show_plans,
        pattern="^plans$",
    )
