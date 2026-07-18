import logging

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

logger = logging.getLogger(__name__)


async def upload_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if context.user_data.get("selected_plan") is None:
        await query.message.reply_text(
            "❌ Please select a subscription plan first."
        )
        return

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

    # Prevent two screenshot handlers for the same user from running in this
    # application process at exactly the same time.
    lock_key = f"payment_upload_lock:{user.id}"
    lock = context.application.bot_data.get(lock_key)

    if lock is None:
        import asyncio

        lock = asyncio.Lock()
        context.application.bot_data[lock_key] = lock

    async with lock:
        photo = update.message.photo[-1].file_id

        duration_minutes = int(plan.get("duration_minutes", 1440))
        duration_text = plan.get("duration_text", "1d")
        plan_name = plan.get("name", "Premium").replace("_", "-")

        try:
            payment = await create_payment(
                user_id=user.id,
                plan=plan_name,
                amount=plan["price"],
                screenshot_file_id=photo,
                duration_minutes=duration_minutes,
                duration_text=duration_text,
            )
        except Exception:
            logger.exception(
                "Payment screenshot submission failed user_id=%s plan=%s",
                user.id,
                plan_name,
            )
            await update.message.reply_text(
                "❌ Payment submission failed temporarily. Please try again."
            )
            return

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
            f"⏳ Duration: {duration_text}\n"
            f"🧾 Payment ID: {payment_id}"
        )

        admins = await get_all_admins()
        notified_admins = 0

        for admin in admins:
            try:
                await context.bot.send_photo(
                    chat_id=admin["admin_id"],
                    photo=photo,
                    caption=caption,
                    reply_markup=keyboard,
                )
                notified_admins += 1
            except Exception:
                logger.exception(
                    "Failed to notify payment admin admin_id=%s "
                    "payment_id=%s user_id=%s",
                    admin.get("admin_id"),
                    payment_id,
                    user.id,
                )

        if notified_admins == 0:
            logger.error(
                "No admin received payment notification payment_id=%s "
                "user_id=%s",
                payment_id,
                user.id,
            )
            await update.message.reply_text(
                "⚠️ Your screenshot was saved, but the admin notification "
                "is delayed. Please do not submit it repeatedly."
            )
            return

        await update.message.reply_text(
            "✅ Payment submitted successfully.\n\n"
            "Waiting for admin approval.\n"
            "Submitting another screenshot for the same plan will update "
            "this pending request instead of creating a duplicate."
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
