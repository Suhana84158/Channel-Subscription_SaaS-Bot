from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database.admins import is_admin
from database.seller_bots import get_bot
from database.sellers import get_seller
from database.users import get_or_create_user


def owner_welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Owner Dashboard", callback_data="main_owner_dashboard")],
        [InlineKeyboardButton("🛡 Owner Admin Panel", callback_data="admin_home")],
        [
            InlineKeyboardButton("👥 Users", callback_data="admin_users"),
            InlineKeyboardButton("🏪 Sellers", callback_data="main_owner_sellers"),
        ],
        [
            InlineKeyboardButton("💳 Payments", callback_data="admin_pending_payments"),
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
        ],
        [InlineKeyboardButton("🆘 Help & Commands", callback_data="main_help")],
    ])


def seller_welcome_keyboard(has_bot: bool):
    rows = []

    if has_bot:
        rows.extend([
            [InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")],
            [InlineKeyboardButton("🤖 Manage My Child Bot", callback_data="seller_my_bot")],
        ])
    else:
        rows.extend([
            [InlineKeyboardButton("➕ Create / Connect Child Bot", callback_data="seller_connect")],
            [InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")],
        ])

    rows.extend([
        [InlineKeyboardButton("📖 Child Bot Setup Guide", callback_data="main_child_setup")],
        [InlineKeyboardButton("🆘 Help & Commands", callback_data="main_help")],
    ])
    return InlineKeyboardMarkup(rows)


async def role_welcome(user_id: int):
    if await is_admin(user_id):
        return (
            "🚀 Main Bot Platform\n\n"
            "👑 Welcome, Owner!\n\n"
            "From this dashboard you can control:\n"
            "• Sellers and their connected child bots\n"
            "• Main-bot users and subscriptions\n"
            "• Payments and transactions\n"
            "• Broadcasts, channels and system settings\n\n"
            "Choose an option below.",
            owner_welcome_keyboard(),
        )

    seller = await get_seller(user_id)
    bot = await get_bot(user_id)
    seller_name = (seller or {}).get("first_name") or "Seller"

    return (
        "🤖 Build Your Subscription Bot\n\n"
        f"Welcome, {seller_name}!\n\n"
        "Create and control your own Telegram subscription bot from here.\n\n"
        "✅ Add plans and prices\n"
        "✅ Connect channels and groups\n"
        "✅ Accept and approve payments\n"
        "✅ Auto-remove expired members\n"
        "✅ Broadcast, referrals and user management\n\n"
        + (
            f"Connected Bot: @{bot.get('bot_username')}\n"
            f"Status: {'🟢 Active' if bot.get('active') else '⏸ Paused'}"
            if bot
            else "No child bot connected yet. Tap the button below to create one."
        ),
        seller_welcome_keyboard(bool(bot)),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = await get_or_create_user(tg_user)

    if user.get("banned"):
        await update.effective_message.reply_text(
            "🚫 You are banned from using this bot.\n\n"
            f"Reason: {user.get('ban_reason') or 'Not specified'}"
        )
        return

    text, keyboard = await role_welcome(tg_user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await get_or_create_user(query.from_user)

    text, keyboard = await role_welcome(query.from_user.id)
    await query.edit_message_text(text, reply_markup=keyboard)


def start_command():
    return CommandHandler("start", start)


def start_callback_handler():
    return CallbackQueryHandler(start_callback, pattern=r"^(start|main_home)$")
