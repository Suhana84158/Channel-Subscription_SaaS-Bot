from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database.admins import is_admin
from database.seller_bots import get_bot
from database.sellers import get_or_create_seller, get_seller
from database.seller_referrals import register_seller_referral, reward_seller_referral
from database.users import get_or_create_user


def owner_welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Owner Dashboard", callback_data="main_owner_dashboard")],
        [
            InlineKeyboardButton("👥 Users", callback_data="admin_users"),
            InlineKeyboardButton("🏪 Sellers", callback_data="main_owner_sellers"),
        ],
        [
            InlineKeyboardButton("💳 Payments", callback_data="admin_pending_payments"),
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
        ],
        [InlineKeyboardButton("🌐 Official Links", callback_data="official_links_open")],
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
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        [InlineKeyboardButton("🌐 Official Links", callback_data="official_links_open")],
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

    # Register seller-to-seller referral from /start refseller_<seller_id>.
    if context.args and context.args[0].startswith("refseller_"):
        try:
            referrer_id = int(context.args[0].replace("refseller_", "", 1))
            if referrer_id != tg_user.id:
                await get_or_create_seller(tg_user)
                referral = await register_seller_referral(referrer_id, tg_user.id)
                reward = await reward_seller_referral(tg_user.id) if referral else None
                if reward and int(reward.get("reward_days", 0)) > 0:
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            "🎉 Seller Referral Reward Added!\n\n"
                            f"A new seller joined using your link.\n"
                            f"Reward: {reward['reward_days']} day(s).",
                        )
                    except Exception:
                        pass
        except (TypeError, ValueError):
            pass

    # Owner should land directly on the full Owner Dashboard.
    if await is_admin(tg_user.id):
        from handlers.main_dashboard import owner_dashboard_keyboard, owner_dashboard_text

        await update.effective_message.reply_text(
            await owner_dashboard_text(),
            reply_markup=owner_dashboard_keyboard(),
        )
        return

    text, keyboard = await role_welcome(tg_user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await get_or_create_user(query.from_user)

    # Old owner Main Menu buttons should also return to Owner Dashboard.
    if await is_admin(query.from_user.id):
        from handlers.main_dashboard import owner_dashboard_keyboard, owner_dashboard_text

        await query.edit_message_text(
            await owner_dashboard_text(),
            reply_markup=owner_dashboard_keyboard(),
        )
        return

    text, keyboard = await role_welcome(query.from_user.id)
    await query.edit_message_text(text, reply_markup=keyboard)


def start_command():
    return CommandHandler("start", start)


def start_callback_handler():
    return CallbackQueryHandler(start_callback, pattern=r"^(start|main_home)$")
