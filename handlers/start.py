import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import ADMIN_IDS
from database.admins import is_admin
from database.seller_bots import get_bot
from database.sellers import get_or_create_seller, get_seller
from database.seller_referrals import register_seller_referral, reward_seller_referral
from database.users import get_or_create_user
from database.referrals import create_referral
from logging_config import get_logger

logger = get_logger(__name__)
DB_TIMEOUT = 8


async def _bounded(awaitable, *, timeout=DB_TIMEOUT, default=None, label="database operation"):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Timed out during %s after %ss", label, timeout)
    except Exception:
        logger.exception("Failed during %s", label)
    return default


async def _is_owner(user_id: int) -> bool:
    # Environment owners must still be able to open the dashboard during a
    # temporary MongoDB slowdown.
    if int(user_id) in ADMIN_IDS:
        return True
    return bool(
        await _bounded(
            is_admin(user_id),
            default=False,
            label="owner check",
        )
    )


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
    rows = [
        [InlineKeyboardButton("🤖 Manage My Clone Bots", callback_data="seller_bots_list")],
        [InlineKeyboardButton("➕ Create New Clone Bot", callback_data="seller_connect")],
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan_home")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        [InlineKeyboardButton("🌐 Official Links", callback_data="official_links_open")],
    ]
    return InlineKeyboardMarkup(rows)


async def role_welcome(user_id: int):
    if await _is_owner(user_id):
        return (
            "🚀 Main Bot Platform\n\n"
            "👑 Welcome, Owner!\n\n"
            "From this dashboard you can control:\n"
            "• Sellers and their connected clone bots\n"
            "• Main-bot users and subscriptions\n"
            "• Payments and transactions\n"
            "• Broadcasts, channels and system settings\n\n"
            "Choose an option below.",
            owner_welcome_keyboard(),
        )

    seller, bot = await asyncio.gather(
        _bounded(get_seller(user_id), default=None, label="seller lookup"),
        _bounded(get_bot(user_id), default=None, label="clone bot lookup"),
    )
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
            else "No clone bot connected yet. Tap the button below to create one."
        ),
        seller_welcome_keyboard(bool(bot)),
    )


async def _send_owner_dashboard(message):
    from handlers.main_dashboard import owner_dashboard_keyboard, owner_dashboard_text

    text = await _bounded(
        owner_dashboard_text(),
        timeout=10,
        default="👑 Owner Dashboard\n\nSelect an option below.",
        label="owner dashboard summary",
    )
    await asyncio.wait_for(
        message.reply_text(text, reply_markup=owner_dashboard_keyboard()),
        timeout=12,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    tg_user = update.effective_user
    if message is None or tg_user is None:
        return

    user = await _bounded(
        get_or_create_user(tg_user),
        default=None,
        label="user registration",
    )

    if user and user.get("banned"):
        await message.reply_text(
            "🚫 You are banned from using this bot.\n\n"
            f"Reason: {user.get('ban_reason') or 'Not specified'}"
        )
        return

    if context.args and context.args[0].isdigit():
        try:
            referrer_id = int(context.args[0])
            if referrer_id != tg_user.id:
                await _bounded(
                    create_referral(referrer_id, tg_user.id),
                    default=None,
                    label="user referral registration",
                )
        except (TypeError, ValueError):
            pass

    if context.args and context.args[0].startswith("refseller_"):
        try:
            referrer_id = int(context.args[0].replace("refseller_", "", 1))
            if referrer_id != tg_user.id:
                await _bounded(
                    get_or_create_seller(tg_user),
                    label="seller referral registration",
                )
                referral = await _bounded(
                    register_seller_referral(referrer_id, tg_user.id),
                    default=None,
                    label="seller referral save",
                )
                reward = (
                    await _bounded(
                        reward_seller_referral(tg_user.id),
                        default=None,
                        label="seller referral reward",
                    )
                    if referral
                    else None
                )
                if reward and int(reward.get("reward_days", 0)) > 0:
                    try:
                        await asyncio.wait_for(
                            context.bot.send_message(
                                referrer_id,
                                "🎉 Seller Referral Reward Added!\n\n"
                                "A new seller joined using your link.\n"
                                f"Reward: {reward['reward_days']} day(s).",
                            ),
                            timeout=8,
                        )
                    except Exception:
                        logger.debug("Referral notification failed", exc_info=True)
        except (TypeError, ValueError):
            pass

    try:
        if await _is_owner(tg_user.id):
            await _send_owner_dashboard(message)
            return

        if context.args and context.args[0] == "sellerplan":
            from handlers.seller import send_seller_upgrade_plan
            await send_seller_upgrade_plan(message, tg_user.id)
            return

        text, keyboard = await role_welcome(tg_user.id)
        await asyncio.wait_for(
            message.reply_text(text, reply_markup=keyboard),
            timeout=12,
        )
    except Exception:
        logger.exception("/start failed user_id=%s", tg_user.id)
        try:
            await message.reply_text(
                "⚠️ Bot is temporarily busy. Please send /start again after a few seconds."
            )
        except Exception:
            logger.exception("Fallback /start reply also failed user_id=%s", tg_user.id)


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return

    try:
        await asyncio.wait_for(query.answer(), timeout=5)
    except Exception:
        logger.debug("Callback acknowledgement failed", exc_info=True)

    await _bounded(
        get_or_create_user(query.from_user),
        default=None,
        label="callback user registration",
    )

    try:
        if await _is_owner(query.from_user.id):
            from handlers.main_dashboard import owner_dashboard_keyboard, owner_dashboard_text

            text = await _bounded(
                owner_dashboard_text(),
                timeout=10,
                default="👑 Owner Dashboard\n\nSelect an option below.",
                label="owner dashboard callback summary",
            )
            await asyncio.wait_for(
                query.edit_message_text(text, reply_markup=owner_dashboard_keyboard()),
                timeout=12,
            )
            return

        text, keyboard = await role_welcome(query.from_user.id)
        await asyncio.wait_for(
            query.edit_message_text(text, reply_markup=keyboard),
            timeout=12,
        )
    except Exception:
        logger.exception("Start callback failed user_id=%s", query.from_user.id)
        try:
            await query.message.reply_text(
                "⚠️ Bot is temporarily busy. Please send /start again."
            )
        except Exception:
            pass


def start_command():
    return CommandHandler("start", start)


def start_callback_handler():
    return CallbackQueryHandler(start_callback, pattern=r"^(start|main_home)$")
