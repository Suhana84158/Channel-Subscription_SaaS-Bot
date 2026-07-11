from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.settings import get_setting_value


async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    support_username = await get_setting_value("support_username", "")
    support_username = (support_username or "").strip()

    keyboard = []

    if support_username:
        clean_username = support_username.lstrip("@")
        text = (
            "📞 Support\n\n"
            f"Contact our support team: @{clean_username}"
        )
        keyboard.append([
            InlineKeyboardButton(
                "💬 Contact Support",
                url=f"https://t.me/{clean_username}",
            )
        ])
    else:
        text = (
            "📞 Support\n\n"
            "Support username has not been configured yet."
        )

    keyboard.append([
        InlineKeyboardButton("⬅ Back", callback_data="start")
    ])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def support_refresh_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await support_handler(update, context)


def support_callback():
    return CallbackQueryHandler(support_handler, pattern=r"^support$")


def support_reply_handler():
    # Separate callback pattern so it does not intercept normal text/admin messages.
    return CallbackQueryHandler(
        support_refresh_handler,
        pattern=r"^support_refresh$",
    )
