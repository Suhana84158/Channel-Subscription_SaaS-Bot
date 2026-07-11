from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import UPI_ID, UPI_NAME
from database.settings import get_setting
from handlers.plans import get_plan


async def get_payment_settings():
    upi_id_setting = await get_setting("upi_id")
    upi_name_setting = await get_setting("upi_name")
    qr_setting = await get_setting("upi_qr_file_id")

    return {
        "upi_id": upi_id_setting.get("value") if upi_id_setting else UPI_ID,
        "upi_name": upi_name_setting.get("value") if upi_name_setting else UPI_NAME,
        "qr_file_id": qr_setting.get("value") if qr_setting else None,
    }


async def buy_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan_key = query.data.replace("buy_", "")
    plan = get_plan(plan_key)

    if not plan:
        await query.answer("Invalid plan selected.", show_alert=True)
        return

    context.user_data["selected_plan"] = plan

    settings = await get_payment_settings()

    text = (
        "💳 *Subscription Payment*\n\n"
        f"📦 Plan: *{plan['name']}*\n"
        f"💰 Amount: ₹{plan['price']}\n"
        f"⏳ Duration: *{plan['duration_text']}*\n\n"
        f"👤 UPI Name: {settings['upi_name']}\n"
        f"🏦 UPI ID: `{settings['upi_id']}`\n\n"
        "After payment upload your payment screenshot."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload Screenshot", callback_data="upload_payment")],
        [InlineKeyboardButton("⬅ Back", callback_data="plans")],
    ])

    if settings["qr_file_id"]:
        await query.message.reply_photo(
            photo=settings["qr_file_id"],
            caption=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )


def payment_handler():
    return CallbackQueryHandler(
        buy_subscription,
        pattern=r"^buy_",
    )
