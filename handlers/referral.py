from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
)

from database.referrals import total_referrals
from config import REFERRAL_BONUS_DAYS


async def referral_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    user = query.from_user

    bot_username = context.bot.username

    referral_link = (
        f"https://t.me/{bot_username}"
        f"?start={user.id}"
    )

    total = await total_referrals(user.id)

    text = (
        "🎁 *Referral Program*\n\n"
        f"👥 Total Referrals: *{total}*\n"
        f"🎉 Reward: *{REFERRAL_BONUS_DAYS} Free Days* per successful referral.\n\n"
        "🔗 Your Referral Link:\n"
        f"`{referral_link}`"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "📤 Share Referral Link",
                url=f"https://t.me/share/url?url={referral_link}",
            )
        ]
    ]

    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


def referral_callback():
    return CallbackQueryHandler(
        referral_handler,
        pattern="^referral$",
    )
