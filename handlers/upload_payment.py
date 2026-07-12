from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database.payments import create_payment
from database.admins import get_all_admins, is_admin


async def upload_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "📷 Please send your payment screenshot in this chat."
    )


async def handle_payment_screenshot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.photo:
        return

    if await is_admin(update.effective_user.id):
        return

    user = update.effective_user
    plan = context.user_data.get("selected_plan")

    if plan is None:
        await update.message.reply_text(
            "❌ Please select a subscription plan first."
        )
        return

    photo = update.message.photo[-1].file_id

    duration_minutes = int(plan.get("duration_minutes", 1440))
    duration_text = plan.get("duration_text", "1d")
    plan_name = plan.get("name", "Premium").replace("_", "-")

    payment = await create_payment(
        user_id=user.id,
        plan=plan_name,
        amount=plan["price"],
        screenshot_file_id=photo,
        duration_minutes=duration_minutes,
        duration_text=duration_text,
    )

    payment_id = str(payment["_id"])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"pay_approve_{payment_id}",
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"pay_reject_{payment_id}",
            ),
        ]
    ])

    caption = (
        "🆕 New Payment\n\n"
        f"👤 User: {user.first_name}\n"
        f"🆔 User ID: {user.id}\n"
        f"📦 Plan: {plan_name}\n"
        f"💰 Amount: ₹{plan['price']}\n"
        f"⏳ Duration: {duration_text}"
    )

    admins = await get_all_admins()

    for admin in admins:
        try:
            await context.bot.send_photo(
                chat_id=admin["admin_id"],
                photo=photo,
                caption=caption,
                reply_markup=keyboard,
            )
        except Exception:
            pass

    await update.message.reply_text(
        "✅ Payment submitted successfully.\n\n"
        "Waiting for admin approval."
    )


def payment_upload_handlers():
    return [
        CallbackQueryHandler(
            upload_payment_callback,
            pattern=r"^upload_payment$",
        ),
        MessageHandler(
            filters.PHOTO,
            handle_payment_screenshot,
        ),
    ]
