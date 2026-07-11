from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def get_main_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "📋 Plans",
                callback_data="plans",
            ),
            InlineKeyboardButton(
                "💳 Buy",
                callback_data="plans",
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 My Profile",
                callback_data="profile",
            ),
            InlineKeyboardButton(
                "🔄 Renew",
                callback_data="plans",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎁 Referral",
                callback_data="referral",
            ),
            InlineKeyboardButton(
                "📞 Support",
                callback_data="support",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)
