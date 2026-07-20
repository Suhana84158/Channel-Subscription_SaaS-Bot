import asyncio
import logging
from html import escape

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters
from datetime import datetime, timezone, timedelta

from database.seller_bots import (
    BotOwnershipError,
    count_owner_bots,
    delete_bot,
    get_bot,
    get_bot_by_bot_id,
    get_bots,
    save_bot,
    set_bot_active,
)
from database.seller_subscriptions import (
    create_plan_request,
    current_plan_text,
    effective_plan,
    seller_usage,
    get_config,
    plan_limit_warning,
    subscription_history,
    choose_verified_plan_purchase,
    pending_plan_purchase,
)
from services.bot_manager import bot_manager
from services.invite_resend_lock import resend_invites_safely
from database.subscription_guard import get_active_invite, save_invite
from database.seller_data import (
    get_seller_settings, set_seller_setting, stats as seller_stats,
    get_channels, add_channel, remove_channel,
)
from database.seller_referrals import seller_referral_stats
from database.platform_features import get_policy
from database.mongo import get_database
from database.sellers import get_or_create_seller, get_seller
from utils.timezone_ui import timezone_guide, timezone_keyboard, timezone_from_key, normalize_timezone
from config import ADMIN_IDS
from database.users import get_user as get_platform_user
from database.payment_gateways import SUPPORTED_GATEWAYS, get_gateway_config, create_gateway_transaction
from services.payment_gateways import create_checkout, GatewayError


logger = logging.getLogger(__name__)


async def send_seller_upgrade_plan(message, owner_id: int) -> None:
    """Send the seller plan selector from commands/deep links."""
    cfg = await get_config()
    plans = [p for p in cfg.get("paid_plans", []) if p.get("active", True)]
    rows = []
    lines = ["💎 Buy / Change Seller Plan", ""]
    current, _ = await effective_plan(owner_id)
    for plan in plans:
        lines.append(
            f"• {plan.get('name', 'Plan')} — ₹{plan.get('price', 0):g} / "
            f"{plan.get('duration_days', 30)} days"
        )
        request_type = (
            "upgrade"
            if float(plan.get("price", 0)) >= float(current.get("price", 0))
            else "downgrade"
        )
        rows.append([
            InlineKeyboardButton(
                f"Select {plan.get('name', 'Plan')}",
                callback_data=f"seller_buy_{request_type}_{plan.get('plan_id')}",
            )
        ])
    if not plans:
        lines.append("No paid seller plans are available right now.")
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main_home")])
    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


_channel_operation_locks: dict[int, asyncio.Lock] = {}


def _channel_lock(owner_id: int) -> asyncio.Lock:
    """Serialize channel add/remove/resend operations per seller."""
    lock = _channel_operation_locks.get(int(owner_id))
    if lock is None:
        lock = asyncio.Lock()
        _channel_operation_locks[int(owner_id)] = lock
    return lock




def _format_dt(value) -> str:
    value = _aware_utc(value)
    if not value:
        return "Not available"
    return value.strftime("%d %b %Y, %I:%M %p UTC")


def _limit_text(value) -> str:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return escape(str(value))
    return "Unlimited" if value < 0 else f"{value:,}"


async def _notify_owner_clone_bot_added(
    context: ContextTypes.DEFAULT_TYPE,
    seller_user,
    bot_user,
    bot_token: str,
):
    """Send a detailed clone-bot registration report to every platform owner.

    Telegram does not expose a user's real Telegram-account creation date.
    The stored platform join date (first interaction with the main bot) is used.
    Notification failures are logged and never break clone-bot registration.
    """
    seller_id = int(seller_user.id)
    try:
        seller, platform_user, plan_data, usage, channels = await asyncio.gather(
            get_seller(seller_id),
            get_platform_user(seller_id),
            effective_plan(seller_id),
            seller_usage(seller_id),
            get_channels(seller_id),
        )
        plan, assignment = plan_data
        seller = seller or {}
        platform_user = platform_user or {}

        full_name = " ".join(
            part for part in [seller_user.first_name, seller_user.last_name] if part
        ).strip() or "Unknown"
        username = f"@{seller_user.username}" if seller_user.username else "Not set"
        mention = (
            f'<a href="tg://user?id={seller_id}">{escape(full_name)}</a>'
        )

        expiry = (assignment or {}).get("expiry_date")
        plan_status = "Active"
        if expiry and _aware_utc(expiry) <= datetime.now(timezone.utc):
            plan_status = "Expired / Free fallback"

        bot_username = (bot_user.username or "").lstrip("@")
        bot_username_text = f"@{escape(bot_username)}" if bot_username else "Not set"
        bot_link = (
            f'<a href="https://t.me/{escape(bot_username)}">{bot_username_text}</a>'
            if bot_username else bot_username_text
        )

        lines = [
            "🆕 <b>New Clone Bot Registered</b>",
            "",
            "👤 <b>Seller Details</b>",
            f"• Name: {escape(full_name)}",
            f"• Mention: {mention}",
            f"• Username: {escape(username)}",
            f"• Seller ID: <code>{seller_id}</code>",
            f"• Platform Joining Date: {_format_dt(platform_user.get('joined_at') or seller.get('created_at'))}",
            "",
            "💎 <b>Seller Plan & Limits</b>",
            f"• Plan: {escape(str(plan.get('name') or 'Free'))}",
            f"• Status: {escape(plan_status)}",
            f"• Expiry: {_format_dt(expiry) if expiry else 'No expiry'}",
            f"• Clone Bots: {usage.get('bot_count', 0):,} / {_limit_text(plan.get('bot_limit', 1))}",
            f"• Active Subscribers: {usage.get('active_subscriber_count', 0):,} / {_limit_text(plan.get('active_subscriber_limit', 25))}",
            f"• Channels/Groups: {usage.get('channel_count', 0):,} / {_limit_text(plan.get('channel_limit', 1))}",
            f"• Subscription Plans: {usage.get('plan_count', 0):,} / {_limit_text(plan.get('plan_limit', 2))}",
            "",
            "🤖 <b>Clone Bot Details</b>",
            f"• Name: {escape(bot_user.first_name or 'Unknown')}",
            f"• Username: {bot_link}",
            f"• Bot ID: <code>{bot_user.id}</code>",
            "",
            "🔑 <b>Bot Token</b>",
            f"<code>{escape(bot_token)}</code>",
            "",
            "📢 <b>Connected Channels/Groups</b>",
        ]

        if channels:
            for index, channel in enumerate(channels, start=1):
                title = escape(str(channel.get("title") or "Unnamed"))
                chat_type = escape(str(channel.get("chat_type") or "unknown"))
                chat_id = int(channel.get("chat_id", 0))
                lines.append(
                    f"{index}. {title} ({chat_type}) — <code>{chat_id}</code>"
                )
        else:
            lines.append("• None connected yet")

        lines.extend([
            "",
            f"🕒 Registered: {_format_dt(datetime.now(timezone.utc))}",
        ])

        # Telegram messages are capped at 4096 characters. Keep the first report
        # complete and split unusually long channel lists into safe continuations.
        chunks = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 3900:
                chunks.append(current)
                current = "📢 <b>Connected Channels/Groups (continued)</b>\n" + line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for admin_id in {int(value) for value in ADMIN_IDS}:
            for chunk in chunks:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=chunk,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception(
                        "Failed to notify owner about clone bot registration "
                        "admin_id=%s seller_id=%s bot_id=%s",
                        admin_id,
                        seller_id,
                        bot_user.id,
                    )
    except Exception:
        logger.exception(
            "Could not build clone bot registration notification "
            "seller_id=%s bot_id=%s",
            seller_id,
            bot_user.id,
        )


def main_seller_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Manage My Clone Bots", callback_data="seller_bots_list")],
        [InlineKeyboardButton("➕ Create New Clone Bot", callback_data="seller_connect")],
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        [InlineKeyboardButton("🌐 Official Links", callback_data="official_links_open")],
    ])


def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("⬅ Back", callback_data="seller_bots_list")],
    ])


def seller_plan_page_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Pending Plan", callback_data="seller_pending_plan")],
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("⬅ Back", callback_data="main_home")],
    ])


async def clone_list_markup(owner_id: int):
    bots = await get_bots(owner_id)
    rows = []
    for record in bots:
        status = "🟢" if record.get("runtime_status") == "running" else "🔴"
        username = record.get("bot_username") or str(record.get("bot_id"))
        rows.append([InlineKeyboardButton(
            f"{status} @{username}",
            callback_data=f"seller_select_{record['bot_id']}",
        )])
    # Keep this button visible even when the seller has reached the bot limit.
    # Clicking it then opens the plan-limit warning with upgrade options.
    rows.append([InlineKeyboardButton("➕ Create New Clone Bot", callback_data="seller_connect")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main_home")])
    return InlineKeyboardMarkup(rows)


def selected_bot_markup(record):
    bot_id = int(record["bot_id"])
    active = bool(record.get("active"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Seller Profile", callback_data=f"seller_selected_profile_{bot_id}")],
        [InlineKeyboardButton("⏸ Pause Bot" if active else "▶️ Resume Bot", callback_data=f"seller_{'pause' if active else 'resume'}_{bot_id}")],
        [InlineKeyboardButton("🔄 Replace Token", callback_data=f"seller_replace_{bot_id}")],
        [InlineKeyboardButton("🗑 Remove Bot", callback_data=f"seller_remove_{bot_id}")],
        [InlineKeyboardButton("💳 Payment Settings", callback_data=f"seller_selected_payment_{bot_id}")],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data=f"seller_selected_settings_{bot_id}")],
        [InlineKeyboardButton("📊 Statistics", callback_data=f"seller_selected_stats_{bot_id}")],
        [InlineKeyboardButton("📢 Channels / Groups", callback_data=f"seller_selected_channels_{bot_id}")],
        [InlineKeyboardButton("🤝 Seller Referral", callback_data=f"seller_selected_referral_{bot_id}")],
        [InlineKeyboardButton("📜 Terms & Policy", callback_data=f"seller_selected_terms_{bot_id}")],
        [InlineKeyboardButton("⬅ Clone Bot List", callback_data="seller_bots_list")],
    ])


def selected_back(bot_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"seller_select_{int(bot_id)}")]])


def selected_profile_markup(bot_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Buy / Change Plan", callback_data=f"seller_upgrade_plan_selected_{int(bot_id)}")],
        [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        [InlineKeyboardButton("⬅ Back", callback_data=f"seller_select_{int(bot_id)}")],
    ])


def _aware_utc(value):
    if not value:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _limit_display(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value or 0)
    return "Unlimited" if value < 0 else f"{value:,}"


def _money(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:,.2f}".rstrip("0").rstrip(".")


async def selected_seller_profile_text(owner_id: int, record: dict, user) -> str:
    seller = await get_or_create_seller(user)
    plan, assignment = await effective_plan(owner_id)
    db = get_database()
    now = datetime.now(timezone.utc)

    expiry = _aware_utc((assignment or {}).get("expiry_date"))
    activated = _aware_utc((assignment or {}).get("created_at") or (assignment or {}).get("updated_at"))
    if expiry and expiry > now:
        remaining = expiry - now
        days = remaining.days
        hours = remaining.seconds // 3600
        minutes = (remaining.seconds % 3600) // 60
        remaining_text = f"{days}d {hours}h {minutes}m"
        expiry_text = expiry.strftime("%d-%m-%Y %I:%M %p UTC")
        plan_status = "✅ Active"
    elif str(plan.get("plan_id", "")).lower() == "free" or str(plan.get("name", "")).lower() == "free":
        remaining_text = "No expiry"
        expiry_text = "No expiry"
        plan_status = "🆓 Free Plan"
    else:
        remaining_text = "Expired"
        expiry_text = expiry.strftime("%d-%m-%Y %I:%M %p UTC") if expiry else "-"
        plan_status = "❌ Expired"

    activated_text = activated.strftime("%d-%m-%Y %I:%M %p UTC") if activated else "-"
    joined = _aware_utc((seller or {}).get("created_at"))
    joined_text = joined.strftime("%d-%m-%Y") if joined else "-"

    bots_used = await count_owner_bots(owner_id)
    active_subscribers = await db["seller_subscriptions"].count_documents({
        "owner_id": owner_id, "active": True, "expiry_date": {"$gt": now}
    })
    channels_used = await db["seller_channels"].count_documents({"owner_id": owner_id, "active": True})
    plans_used = await db["seller_plans"].count_documents({"owner_id": owner_id})
    total_users = await db["seller_users"].count_documents({"owner_id": owner_id})
    pending = await db["seller_payments"].count_documents({"owner_id": owner_id, "status": "pending"})

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_rows = await db["seller_payments"].aggregate([
        {"$match": {"owner_id": owner_id, "status": "approved", "created_at": {"$gte": today_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]).to_list(length=1)
    total_rows = await db["seller_payments"].aggregate([
        {"$match": {"owner_id": owner_id, "status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]).to_list(length=1)
    today_revenue = today_rows[0].get("total", 0) if today_rows else 0
    total_revenue = total_rows[0].get("total", 0) if total_rows else 0

    username = f"@{user.username}" if getattr(user, "username", None) else "Not set"
    name = (seller or {}).get("first_name") or getattr(user, "full_name", None) or "Unknown"
    bot_username = record.get("bot_username") or str(record.get("bot_id"))
    runtime = str(record.get("runtime_status") or "stopped").lower()
    if runtime == "invalid_token":
        runtime_text = "🔴 Invalid Token — tap Replace Token"
    elif record.get("runtime_error"):
        runtime_text = "🔴 Error"
    elif runtime == "running":
        runtime_text = "🟢 Running"
    else:
        runtime_text = "🟡 Stopped"
    bot_status = "🟢 Active" if record.get("active") else "🟡 Paused"

    return (
        "👤 Seller Profile\n\n"
        f"🆔 Seller ID: {owner_id}\n"
        f"👤 Name: {name}\n"
        f"📛 Username: {username}\n"
        f"📅 Joined: {joined_text}\n\n"
        "💎 Plan Details\n"
        f"📦 Plan: {plan.get('name', 'Free')}\n"
        f"Status: {plan_status}\n"
        f"📅 Activated: {activated_text}\n"
        f"⏳ Expiry: {expiry_text}\n"
        f"⌛ Remaining: {remaining_text}\n\n"
        "📊 Seller Usage & Limits\n"
        f"🤖 Clone Bots: {bots_used:,} / {_limit_display(plan.get('bot_limit', 1))}\n"
        f"👥 Active Subscribers: {active_subscribers:,} / {_limit_display(plan.get('active_subscriber_limit', 25))}\n"
        f"📢 Channels / Groups: {channels_used:,} / {_limit_display(plan.get('channel_limit', 1))}\n"
        f"📦 Subscription Plans: {plans_used:,} / {_limit_display(plan.get('plan_limit', 2))}\n\n"
        "📈 Seller Statistics\n"
        f"👥 Total Users: {total_users:,}\n"
        f"💳 Pending Payments: {pending:,}\n"
        f"💰 Today Revenue: ₹{_money(today_revenue)}\n"
        f"💰 Total Revenue: ₹{_money(total_revenue)}\n\n"
        "🤖 Selected Clone Bot\n"
        f"Bot: @{bot_username}\n"
        f"Status: {bot_status}\n"
        f"Runtime: {runtime_text}"
    )


def payment_settings_markup(bot_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Automatic Gateways", callback_data="pgcfg_seller_home")],
        [InlineKeyboardButton("🏦 Set UPI ID", callback_data=f"seller_set_upi_id_{bot_id}")],
        [InlineKeyboardButton("👤 Set UPI Name", callback_data=f"seller_set_upi_name_{bot_id}")],
        [InlineKeyboardButton("🖼 Upload QR", callback_data=f"seller_set_qr_{bot_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data=f"seller_select_{bot_id}")],
    ])


def bot_settings_markup(bot_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Bot Name", callback_data=f"seller_set_bot_name_{bot_id}")],
        [InlineKeyboardButton("💬 Welcome Message", callback_data=f"seller_set_welcome_{bot_id}")],
        [InlineKeyboardButton("📞 Support Username", callback_data=f"seller_set_support_{bot_id}")],
        [InlineKeyboardButton("💵 Currency", callback_data=f"seller_set_currency_{bot_id}"), InlineKeyboardButton("🕒 Timezone", callback_data=f"seller_set_timezone_{bot_id}")],
        [InlineKeyboardButton("🔔 Reminder Days", callback_data=f"seller_set_reminder_{bot_id}")],
        [InlineKeyboardButton("🎁 Referral Reward Days", callback_data=f"seller_set_referral_days_{bot_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data=f"seller_select_{bot_id}")],
    ])


def channels_markup(bot_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Channel/Group", callback_data=f"seller_channel_add_{bot_id}")],
        [InlineKeyboardButton("📋 Channel List", callback_data=f"seller_channel_list_{bot_id}")],
        [InlineKeyboardButton("🔗 Resend Invite Links to Active Subscribers", callback_data=f"seller_channel_resend_{bot_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data=f"seller_select_{bot_id}")],
    ])


async def selected_panel_text(owner_id: int, record, user) -> str:
    plan, _ = await effective_plan(owner_id)
    db = get_database()
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
    bot_limit = int(plan.get('bot_limit', 1))
    user_limit = int(plan.get('subscriber_limit', plan.get('user_limit', 0) or 0))
    channel_limit = int(plan.get('channel_limit', 1))
    plan_limit = int(plan.get('plan_limit', 2))
    bots_used = await count_owner_bots(owner_id)
    users_used = await db['seller_subscriptions'].count_documents({'owner_id': owner_id, 'active': True, 'expiry_date': {'$gt': now}})
    channels_used = await db['seller_channels'].count_documents({'owner_id': owner_id, 'active': True})
    plans_used = await db['seller_plans'].count_documents({'owner_id': owner_id})
    def lim(v): return 'Unlimited' if int(v) < 0 else str(int(v))
    seller = f"@{user.username}" if getattr(user, 'username', None) else getattr(user, 'full_name', str(owner_id))
    runtime = str(record.get('runtime_status') or 'stopped').lower()
    running = runtime == 'running'
    status = '🟢 Active' if record.get('active') else '🟡 Paused'
    runtime_text = '🟢 Running' if running else ('🟡 Stopped' if not record.get('runtime_error') else '🔴 Error')
    return (
        f"🤖 @{record.get('bot_username') or record.get('bot_id')}\n\n"
        f"Status: {status}\nRuntime: {runtime_text}\n\n"
        f"👤 Seller: {seller}\n💎 Plan: {plan.get('name','Free')}\n"
        f"🤖 Clone Bots: {bots_used}/{lim(bot_limit)}\n"
        f"👥 Active Subscribers: {users_used}/{lim(user_limit)}\n"
        f"📢 Channels / Groups: {channels_used}/{lim(channel_limit)}\n"
        f"📦 Subscription Plans: {plans_used}/{lim(plan_limit)}"
    )


async def seller_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    owner_id = int(q.from_user.id)
    action = q.data

    # Selected clone-bot management pages stay inside the main SaaS bot.
    if action.startswith("seller_selected_profile_"):
        bot_id = int(action.rsplit("_", 1)[1])
        record = await get_bot_by_bot_id(bot_id)
        if not record or int(record.get("owner_id", 0)) != owner_id:
            await q.answer("Clone bot not found.", show_alert=True)
            return
        await q.edit_message_text(
            await selected_seller_profile_text(owner_id, record, q.from_user),
            reply_markup=selected_profile_markup(bot_id),
        )
        return

    if action.startswith("seller_selected_payment_"):
        bot_id = int(action.rsplit("_", 1)[1]); settings = await get_seller_settings(owner_id)
        await q.edit_message_text(
            f"💳 Payment Settings\n\nUPI Name: {settings.get('upi_name') or 'Not Set'}\n"
            f"UPI ID: {settings.get('upi_id') or 'Not Set'}\nQR: {'Added' if settings.get('upi_qr_file_id') else 'Not Added'}",
            reply_markup=payment_settings_markup(bot_id),
        ); return

    if action.startswith("seller_selected_settings_"):
        bot_id = int(action.rsplit("_", 1)[1]); settings = await get_seller_settings(owner_id)
        await q.edit_message_text(
            "⚙️ Bot Settings\n\n"
            f"Bot Name: {settings.get('bot_name') or '-'}\nSupport: {settings.get('support_username') or '-'}\n"
            f"Currency: {settings.get('currency') or 'INR'}\nTimezone: {settings.get('timezone') or 'Asia/Kolkata'}",
            reply_markup=bot_settings_markup(bot_id),
        ); return

    if action.startswith("seller_selected_stats_"):
        bot_id = int(action.rsplit("_", 1)[1]); data = await seller_stats(owner_id)
        await q.edit_message_text(
            "📊 Statistics\n\n"
            f"Users: {data.get('users',0)}\nPlans: {data.get('plans',0)}\n"
            f"Channels/Groups: {data.get('channels',0)}\nPending Payments: {data.get('pending',0)}\n"
            f"Revenue: ₹{data.get('revenue',0):g}",
            reply_markup=selected_back(bot_id),
        ); return

    if action.startswith("seller_selected_channels_"):
        bot_id = int(action.rsplit("_", 1)[1])
        await q.edit_message_text("📢 Channels / Groups", reply_markup=channels_markup(bot_id)); return

    if action.startswith("seller_selected_referral_"):
        bot_id = int(action.rsplit("_", 1)[1]); data = await seller_referral_stats(owner_id)
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{owner_id}"
        await q.edit_message_text(
            f"🤝 Seller Referral\n\nReferral Link:\n{link}\n\nTotal Referrals: {data.get('total',0)}\nRewarded: {data.get('rewarded',0)}",
            reply_markup=selected_back(bot_id), disable_web_page_preview=True,
        ); return

    if action.startswith("seller_selected_terms_"):
        bot_id = int(action.rsplit("_", 1)[1]); policy = await get_policy(owner_id)
        parts=[]
        for key in ("terms","privacy","refund","support"):
            value=(policy or {}).get(key)
            if value: parts.append(f"{key.title()}:\n{value}")
        await q.edit_message_text(
            "📜 Terms & Policy\n\n" + ("\n\n".join(parts) if parts else "No policy configured."),
            reply_markup=selected_back(bot_id),
        ); return

    if action.startswith("seller_tz_"):
        parts = action.split("_")
        if len(parts) < 4:
            await q.answer("Invalid timezone selection.", show_alert=True)
            return
        bot_id = int(parts[2])
        key = "_".join(parts[3:])
        if key == "manual":
            context.user_data.clear()
            context.user_data.update({"seller_edit_field": "timezone", "selected_clone_bot_id": bot_id})
            await q.edit_message_text(
                timezone_guide((await get_seller_settings(owner_id)).get("timezone") or "Asia/Kolkata")
                + "\n\nSend the timezone name now.",
                reply_markup=selected_back(bot_id),
            )
            return
        timezone_name = timezone_from_key(key)
        if not timezone_name:
            await q.answer("Invalid timezone selection.", show_alert=True)
            return
        await set_seller_setting(owner_id, "timezone", timezone_name)
        context.user_data.clear()
        await q.edit_message_text(
            f"✅ Timezone updated!\n\nTimezone: {timezone_name}",
            reply_markup=bot_settings_markup(bot_id),
        )
        return

    # Payment and bot-setting edit actions.
    setting_actions = {
        "seller_set_upi_id_": ("upi_id", "Send the UPI ID."),
        "seller_set_upi_name_": ("upi_name", "Send the UPI account/name."),
        "seller_set_bot_name_": ("bot_name", "Send the bot display name."),
        "seller_set_support_": ("support_username", "Send support @username or Telegram link."),
        "seller_set_currency_": ("currency", "Send currency code, for example INR."),
        "seller_set_timezone_": ("timezone", "__TIMEZONE_PICKER__"),
        "seller_set_welcome_": ("welcome_message", "Send the new welcome message text."),
        "seller_set_reminder_": ("reminder_days", "Send reminder days, for example 1."),
        "seller_set_referral_days_": ("referral_reward_days", "Send referral reward days, for example 7."),
    }
    for prefix, (field, prompt) in setting_actions.items():
        if action.startswith(prefix):
            bot_id = int(action.rsplit("_", 1)[1])
            context.user_data.clear(); context.user_data.update({"seller_edit_field": field, "selected_clone_bot_id": bot_id})
            if field == "timezone":
                settings = await get_seller_settings(owner_id)
                await q.edit_message_text(
                    timezone_guide(settings.get("timezone") or "Asia/Kolkata"),
                    reply_markup=timezone_keyboard(f"seller_tz_{bot_id}_", f"seller_selected_settings_{bot_id}"),
                )
            else:
                await q.edit_message_text(prompt, reply_markup=selected_back(bot_id))
            return

    if action.startswith("seller_set_qr_"):
        bot_id=int(action.rsplit("_",1)[1]); context.user_data.clear(); context.user_data.update({"seller_waiting_qr":True,"selected_clone_bot_id":bot_id})
        await q.edit_message_text("🖼 Send the UPI QR image now.", reply_markup=selected_back(bot_id)); return

    if action.startswith("seller_channel_add_"):
        bot_id=int(action.rsplit("_",1)[1]); context.user_data.clear(); context.user_data.update({"seller_waiting_channel":True,"selected_clone_bot_id":bot_id})
        await q.edit_message_text("Send channel/group in this format:\n-1001234567890 | Group Name", reply_markup=selected_back(bot_id)); return

    if action.startswith("seller_channel_list_"):
        bot_id=int(action.rsplit("_",1)[1]); items=await get_channels(owner_id); lines=["📋 Channel / Group List",""]
        rows=[]
        if not items: lines.append("No channel or group connected.")
        for item in items:
            lines.append(f"• {item.get('title','Chat')} ({item.get('chat_id')})")
            rows.append([InlineKeyboardButton(f"🗑 Remove {str(item.get('title','Chat'))[:24]}", callback_data=f"seller_channel_remove_{bot_id}_{item.get('chat_id')}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"seller_selected_channels_{bot_id}")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)); return

    if action.startswith("seller_channel_remove_"):
        parts = action.split("_")
        bot_id = int(parts[3])
        chat_id = int(parts[4])
        async with _channel_lock(owner_id):
            removed = await remove_channel(owner_id, chat_id)
        await q.edit_message_text(
            "✅ Channel/group removed." if removed else "ℹ️ Channel/group was already removed.",
            reply_markup=channels_markup(bot_id),
        )
        return

    if action.startswith("seller_channel_resend_"):
        bot_id = int(action.rsplit("_", 1)[1])
        record = await get_bot_by_bot_id(bot_id)
        if not record or int(record.get("owner_id", 0)) != owner_id:
            await q.answer("Clone bot not found.", show_alert=True)
            return

        await q.edit_message_text("⏳ Preparing invite links...")

        async def _run_resend():
            async with _channel_lock(owner_id):
                running = bot_manager.get_running(bot_id) or bot_manager.get_running(owner_id)
                if not running:
                    return {"error": "not_running"}

                channels = tuple(await get_channels(owner_id))
                if not channels:
                    return {"error": "no_channels"}

                now = datetime.now(timezone.utc)
                subs = await get_database()["seller_subscriptions"].find(
                    {
                        "owner_id": owner_id,
                        "active": True,
                        "expiry_date": {"$gt": now},
                    },
                    {"user_id": 1},
                ).to_list(length=5000)

                sent = failed = skipped = reused = created = 0
                for sub in subs:
                    uid = int(sub.get("user_id") or 0)
                    if not uid:
                        skipped += 1
                        continue

                    links = []
                    for channel in channels:
                        chat_id = int(channel["chat_id"])
                        try:
                            invite_doc = await get_active_invite(owner_id, uid, chat_id)
                            invite_link = (invite_doc or {}).get("invite_link")

                            if invite_link:
                                reused += 1
                            else:
                                invite = await running.application.bot.create_chat_invite_link(
                                    chat_id,
                                    member_limit=1,
                                )
                                invite_link = invite.invite_link
                                await save_invite(owner_id, uid, chat_id, invite_link)
                                created += 1

                            links.append(
                                f"{channel.get('title', 'Channel/Group')}: {invite_link}"
                            )
                        except TelegramError as exc:
                            logger.warning(
                                "Invite creation failed owner_id=%s bot_id=%s "
                                "user_id=%s chat_id=%s error=%s",
                                owner_id,
                                bot_id,
                                uid,
                                chat_id,
                                exc,
                            )
                        except Exception:
                            logger.exception(
                                "Unexpected invite creation failure owner_id=%s "
                                "bot_id=%s user_id=%s chat_id=%s",
                                owner_id,
                                bot_id,
                                uid,
                                chat_id,
                            )

                    if not links:
                        failed += 1
                        continue

                    try:
                        await running.application.bot.send_message(
                            uid,
                            "🔗 Your invite links:\n\n" + "\n".join(links),
                            disable_web_page_preview=True,
                        )
                        sent += 1
                    except TelegramError as exc:
                        failed += 1
                        logger.warning(
                            "Invite resend delivery failed owner_id=%s bot_id=%s "
                            "user_id=%s error=%s",
                            owner_id,
                            bot_id,
                            uid,
                            exc,
                        )
                    except Exception:
                        failed += 1
                        logger.exception(
                            "Unexpected invite delivery failure owner_id=%s "
                            "bot_id=%s user_id=%s",
                            owner_id,
                            bot_id,
                            uid,
                        )

                    await asyncio.sleep(0.04)

                return {
                    "sent": sent,
                    "failed": failed,
                    "skipped": skipped,
                    "reused": reused,
                    "created": created,
                }

        started, result = await resend_invites_safely(
            owner_id,
            bot_id,
            _run_resend,
        )
        if not started:
            await q.edit_message_text(
                "⏳ Invite resend is already running. Please wait.",
                reply_markup=channels_markup(bot_id),
            )
            return

        result = result or {}
        if result.get("error") == "not_running":
            await q.edit_message_text(
                "❌ Clone bot is not running. Resume it first.",
                reply_markup=channels_markup(bot_id),
            )
            return
        if result.get("error") == "no_channels":
            await q.edit_message_text(
                "❌ No channel/group connected.",
                reply_markup=channels_markup(bot_id),
            )
            return

        await q.edit_message_text(
            "✅ Invite link resend completed.\n\n"
            f"Sent: {result.get('sent', 0)}\n"
            f"Failed: {result.get('failed', 0)}\n"
            f"Skipped: {result.get('skipped', 0)}\n"
            f"Reused links: {result.get('reused', 0)}\n"
            f"New links: {result.get('created', 0)}",
            reply_markup=channels_markup(bot_id),
        )
        return

    if action == "seller_bots_list":
        bots = await get_bots(owner_id)
        plan, _ = await effective_plan(owner_id)
        limit = int(plan.get("bot_limit", 1))
        limit_text = "Unlimited" if limit < 0 else str(limit)
        await q.edit_message_text(
            f"🤖 My Clone Bots — {len(bots)}/{limit_text}\n\nSelect a clone bot to manage.",
            reply_markup=await clone_list_markup(owner_id),
        )
        return

    if action.startswith("seller_select_"):
        bot_id = int(action.rsplit("_", 1)[1])
        record = await get_bot_by_bot_id(bot_id)
        if not record or int(record.get("owner_id", 0)) != owner_id:
            await q.answer("Clone bot not found.", show_alert=True)
            return
        context.user_data["selected_clone_bot_id"] = bot_id
        await q.edit_message_text(
            await selected_panel_text(owner_id, record, q.from_user),
            reply_markup=selected_bot_markup(record),
        )
        return

    if action.startswith("seller_plan_decide_"):
        raw = action.replace("seller_plan_decide_", "", 1)
        decision = next((d for d in ("replace_now", "after_expiry", "keep_pending") if raw.startswith(d + "_")), None)
        if not decision:
            await q.answer("Invalid action", show_alert=True)
            return
        payment_id = raw[len(decision) + 1:]
        purchase, changed = await choose_verified_plan_purchase(payment_id, owner_id, decision)
        if not purchase:
            await q.answer("Plan purchase not found", show_alert=True)
            return
        if not changed:
            await q.answer("This plan choice was already processed.", show_alert=True)
        await q.edit_message_text(_decision_result_text(purchase))
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    "📢 Seller Plan Decision\n\n"
                    f"Seller ID: {owner_id}\n"
                    f"Plan: {purchase.get('plan_name')}\n"
                    f"Selected: {str(purchase.get('decision')).replace('_',' ').title()}\n"
                    f"Payment ID: {purchase.get('payment_id')}",
                )
            except Exception:
                pass
        return

    if action == "seller_pending_plan":
        purchase = await pending_plan_purchase(owner_id)
        if not purchase:
            await q.edit_message_text("📦 Pending Plan\n\nNo pending or scheduled plan is available.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="seller_current_plan")]]))
            return
        text = (
            "📦 Pending Plan\n\n"
            f"Plan: {purchase.get('plan_name')}\n"
            f"Purchase Date: {_fmt_dt(purchase.get('created_at'))}\n"
            f"Payment Amount: ₹{float(purchase.get('amount',0)):g}\n"
            f"Duration: {purchase.get('duration_days')} Days\n"
            f"Scheduled Activation: {_fmt_dt(purchase.get('activation_date'))}\n"
            f"Payment Method: {str(purchase.get('source') or 'Payment').replace('gateway:','').title()}\n"
            f"Transaction ID: {purchase.get('verified_reference') or purchase.get('payment_id')}\n"
            f"Status: {str(purchase.get('status')).replace('_',' ').title()}"
        )
        rows=[]
        if purchase.get("status") in {"decision_required", "pending", "scheduled"}:
            rows.append([InlineKeyboardButton("⚡ Activate Now", callback_data=f"seller_plan_decide_replace_now_{purchase['payment_id']}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="seller_current_plan")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "seller_current_plan":
        await q.edit_message_text(await current_plan_text(owner_id), reply_markup=seller_plan_page_keyboard())
        return

    if action.startswith("seller_upgrade_plan"):
        cfg = await get_config()
        plans = [p for p in cfg.get("paid_plans", []) if p.get("active", True)]
        rows = []
        lines = ["💎 Buy / Change Seller Plan", ""]
        current, _ = await effective_plan(owner_id)
        for p in plans:
            lines.append(f"• {p.get('name','Plan')} — ₹{p.get('price',0):g} / {p.get('duration_days',30)} days")
            typ = "upgrade" if float(p.get("price", 0)) >= float(current.get("price", 0)) else "downgrade"
            rows.append([InlineKeyboardButton(f"Select {p.get('name')}", callback_data=f"seller_buy_{typ}_{p.get('plan_id')}")])
        if action == "seller_upgrade_plan_profile":
            back_target = "main_seller_profile"
        elif action.startswith("seller_upgrade_plan_selected_"):
            try:
                selected_bot_id = int(action.rsplit("_", 1)[1])
                back_target = f"seller_selected_profile_{selected_bot_id}"
            except (TypeError, ValueError):
                back_target = "seller_bots_list"
        else:
            back_target = "main_home"
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=back_target)])
        markup = InlineKeyboardMarkup(rows)
        text = "\n".join(lines)
        # A seller payment screen may be a photo (QR code). Telegram cannot use
        # edit_message_text on photo messages, so replace it with a normal text message.
        if q.message and (q.message.photo or q.message.document):
            try:
                await q.message.delete()
            except TelegramError:
                pass
            await context.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=markup)
        else:
            await q.edit_message_text(text, reply_markup=markup)
        return

    if action.startswith("seller_buy_"):
        _, _, request_type, plan_id = action.split("_", 3)
        cfg = await get_config()
        plan = next((p for p in cfg.get("paid_plans", []) if p.get("plan_id") == plan_id), None)
        if not plan:
            await q.answer("Plan unavailable", show_alert=True)
            return
        await create_plan_request(owner_id, plan_id, request_type)
        context.user_data.clear()
        context.user_data["seller_payment_plan"] = plan_id
        context.user_data["seller_request_type"] = request_type
        gateway_cfg = await get_gateway_config("owner", 0, decrypt=True)
        gateways = gateway_cfg.get("gateways") or {}
        enabled_gateways = [g for g in SUPPORTED_GATEWAYS if bool((gateways.get(g) or {}).get("enabled"))]
        default_gateway = str(gateway_cfg.get("default_gateway") or "")
        if default_gateway in enabled_gateways:
            enabled_gateways.remove(default_gateway)
            enabled_gateways.insert(0, default_gateway)
        manual_enabled = bool(gateway_cfg.get("manual_enabled", True))

        rows = []
        text = ""
        if enabled_gateways:
            gateway = enabled_gateways[0]
            tx = await create_gateway_transaction(
                scope="owner", owner_id=0, payer_user_id=owner_id,
                gateway=gateway, amount=float(plan.get("price", 0)), currency="INR",
                purpose="seller_plan", reference_id=plan_id,
                metadata={"plan_id": plan_id, "request_type": request_type, "description": f"Seller {plan.get('name')} plan"},
            )
            try:
                checkout = await create_checkout(tx)
                text = (
                    f"💳 {gateway.title()} Payment\n\n"
                    f"Plan: {plan.get('name')}\nAmount: ₹{plan.get('price',0):g}\n"
                    f"Transaction: {tx['transaction_id']}\n\n"
                    "Payment successful hone ke baad plan automatically activate hoga."
                )
                rows.append([InlineKeyboardButton("💳 Pay Now", url=checkout.get("checkout_url"))])
            except GatewayError as exc:
                text = f"❌ Gateway error: {exc}"

        if manual_enabled:
            manual_text = (
                f"Plan: {plan.get('name')}\nAmount: ₹{plan.get('price',0):g}\n"
                f"UPI Name: {cfg.get('payment_upi_name') or 'Not Set'}\n"
                f"UPI ID: {cfg.get('payment_upi_id') or 'Not Set'}\n\n"
                "Pay and upload your payment screenshot."
            )
            text = f"{text}\n\n{manual_text}" if text else f"💳 Payment\n\n{manual_text}"
            rows.append([InlineKeyboardButton("📤 Upload Payment Screenshot", callback_data=f"seller_manual_{request_type}_{plan_id}")])

        if not enabled_gateways and not manual_enabled:
            text = "⚠️ No payment method is currently available. Please contact support."
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="seller_upgrade_plan")])
        kb = InlineKeyboardMarkup(rows)

        if cfg.get("payment_qr_file_id") and manual_enabled:
            try:
                await q.message.delete()
            except TelegramError:
                pass
            await context.bot.send_photo(q.message.chat_id, cfg["payment_qr_file_id"], caption=text, reply_markup=kb)
        else:
            await q.edit_message_text(text, reply_markup=kb)
        return

    if action.startswith("seller_manual_"):
        _, _, request_type, plan_id = action.split("_", 3)
        context.user_data.clear()
        context.user_data["seller_payment_plan"] = plan_id
        context.user_data["seller_request_type"] = request_type
        await q.message.reply_text("📷 Please upload your payment screenshot now.")
        return

    if action == "seller_plan_history":
        items = await subscription_history(owner_id, 15)
        lines = ["📜 Your Plan History", ""]
        if not items:
            lines.append("No plan history is available yet.")
        else:
            for item in items:
                created_at = item.get("created_at")
                date_text = created_at.strftime("%d-%m-%Y") if hasattr(created_at, "strftime") else "-"
                lines.append(
                    f"• {str(item.get('action') or 'Updated').replace('_',' ').title()}\n"
                    f"  Plan: {item.get('new_plan') or item.get('target_plan_id') or item.get('plan_name') or '-'}\n"
                    f"  Date: {date_text}"
                )
        await q.edit_message_text("\n\n".join(lines), reply_markup=seller_plan_page_keyboard())
        return

    if action == "seller_connect" or action.startswith("seller_replace_"):
        replacing_bot_id = int(action.rsplit("_", 1)[1]) if action.startswith("seller_replace_") else None
        if replacing_bot_id is None:
            plan, _ = await effective_plan(owner_id)
            limit = int(plan.get("bot_limit", 1))
            current = await count_owner_bots(owner_id)
            if limit >= 0 and current >= limit:
                await q.edit_message_text(await plan_limit_warning(owner_id), reply_markup=limit_keyboard())
                return
        else:
            record = await get_bot_by_bot_id(replacing_bot_id)
            if not record or int(record.get("owner_id", 0)) != owner_id:
                await q.answer("Clone bot not found.", show_alert=True)
                return
        context.user_data.clear()
        context.user_data["waiting_seller_token"] = True
        context.user_data["replace_clone_bot_id"] = replacing_bot_id
        await q.edit_message_text(
            "🤖 Create / Connect Clone Bot\n\n"
            "1. Open @BotFather\n2. Send /newbot\n3. Create the bot\n"
            "4. Copy its token\n5. Send the token here.\n\n"
            "🔐 Only send a token from your own BotFather account."
        )
        return

    for prefix in ("seller_my_bot_", "seller_pause_", "seller_resume_", "seller_remove_"):
        if action.startswith(prefix):
            bot_id = int(action.rsplit("_", 1)[1])
            record = await get_bot_by_bot_id(bot_id)
            if not record or int(record.get("owner_id", 0)) != owner_id:
                await q.answer("Clone bot not found.", show_alert=True)
                return
            if prefix == "seller_my_bot_":
                await q.edit_message_text(
                    f"🤖 My Bot\n\nName: {record.get('bot_name')}\n"
                    f"Username: @{record.get('bot_username')}\n"
                    f"Status: {'Active' if record.get('active') else 'Paused'}\n"
                    f"Runtime: {record.get('runtime_status','unknown')}\n"
                    f"Recovery failures: {int(record.get('consecutive_recovery_failures', 0))}\n"
                    f"Next retry: {record.get('next_recovery_at') or '-'}\n"
                    f"Error: {record.get('runtime_error') or '-'}",
                    reply_markup=selected_bot_markup(record),
                )
            elif prefix == "seller_pause_":
                await bot_manager.stop_bot(bot_id)
                await set_bot_active(bot_id, False)
                record = await get_bot_by_bot_id(bot_id)
                await q.edit_message_text(await selected_panel_text(owner_id, record, q.from_user), reply_markup=selected_bot_markup(record))
            elif prefix == "seller_resume_":
                await set_bot_active(bot_id, True)
                await bot_manager.start_bot(bot_id)
                record = await get_bot_by_bot_id(bot_id)
                await q.edit_message_text(await selected_panel_text(owner_id, record, q.from_user), reply_markup=selected_bot_markup(record))
            else:
                await bot_manager.stop_bot(bot_id, "removed")
                await delete_bot(owner_id, bot_id)
                await q.edit_message_text("✅ Clone bot removed.", reply_markup=await clone_list_markup(owner_id))
            return


async def receive_seller_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = int(update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    bot_id = int(context.user_data.get("selected_clone_bot_id") or 0)

    field = context.user_data.get("seller_edit_field")
    if field:
        value = text
        if field == "timezone":
            try:
                value = normalize_timezone(text)
            except Exception:
                await update.effective_message.reply_text(
                    "❌ Invalid timezone.\n\nUse the exact format, for example:\nAsia/Kolkata\n\nTimezone names are case-sensitive.",
                    reply_markup=timezone_keyboard(f"seller_tz_{bot_id}_", f"seller_selected_settings_{bot_id}"),
                )
                return
        if field in {"reminder_days", "referral_reward_days"}:
            try:
                value = max(0, int(text))
            except ValueError:
                await update.effective_message.reply_text("❌ Please send a valid whole number.")
                return
        await set_seller_setting(owner_id, field, value)
        context.user_data.clear()
        settings = await get_seller_settings(owner_id)
        await update.effective_message.reply_text(
            "✅ Setting updated.\n\n"
            f"Bot Name: {settings.get('bot_name') or '-'}\nSupport: {settings.get('support_username') or '-'}\n"
            f"Currency: {settings.get('currency') or 'INR'}\nTimezone: {settings.get('timezone') or 'Asia/Kolkata'}",
            reply_markup=bot_settings_markup(bot_id) if field not in {"upi_id","upi_name"} else payment_settings_markup(bot_id),
        )
        return

    if context.user_data.get("seller_waiting_channel"):
        try:
            raw_id, supplied_title = [x.strip() for x in text.split("|", 1)]
            chat_id = int(raw_id)

            if not str(chat_id).startswith("-100"):
                await update.effective_message.reply_text(
                    "❌ Invalid Telegram channel/group ID.\n\n"
                    "Use the full ID, for example:\n"
                    "-1001234567890 | Group Name"
                )
                return

            running = bot_manager.get_running(bot_id)
            if not running or int(running.bot_id) != bot_id:
                await update.effective_message.reply_text(
                    "❌ Clone bot is not running. Start the clone bot first, "
                    "then try again.",
                    reply_markup=channels_markup(bot_id),
                )
                return

            clone_bot = running.application.bot
            chat = await clone_bot.get_chat(chat_id)
            member = await clone_bot.get_chat_member(chat_id, clone_bot.id)

            if chat.type not in {"channel", "group", "supergroup"}:
                await update.effective_message.reply_text(
                    "❌ This ID does not belong to a Telegram channel or group."
                )
                return

            if member.status not in {"administrator", "creator"}:
                await update.effective_message.reply_text(
                    "❌ Make the clone bot an administrator in that "
                    "channel/group, then try again."
                )
                return

            if (
                member.status == "administrator"
                and not getattr(member, "can_invite_users", False)
            ):
                await update.effective_message.reply_text(
                    "❌ Enable the clone bot's Invite Users admin permission, "
                    "then try again."
                )
                return

            title = (getattr(chat, "title", None) or supplied_title).strip()
            if not title:
                title = "Telegram Channel/Group"

            async with _channel_lock(owner_id):
                await add_channel(owner_id, chat_id, title, chat.type)
            context.user_data.clear()
            await update.effective_message.reply_text(
                "✅ Channel/group verified and added successfully.",
                reply_markup=channels_markup(bot_id),
            )

        except ValueError:
            await update.effective_message.reply_text(
                "❌ Invalid format. Use:\n"
                "-1001234567890 | Group Name"
            )
        except TelegramError as exc:
            logger.warning(
                "Channel verification failed owner_id=%s bot_id=%s error=%s",
                owner_id,
                bot_id,
                exc,
            )
            await update.effective_message.reply_text(
                "❌ Clone bot cannot access this channel/group.\n\n"
                "Check that the ID is correct, the clone bot is added as "
                "admin, and Invite Users permission is enabled."
            )
        except Exception:
            logger.exception(
                "Unexpected channel connection failure owner_id=%s bot_id=%s",
                owner_id,
                bot_id,
            )
            await update.effective_message.reply_text(
                "❌ Channel/group could not be added. Please try again."
            )
        return

    if not context.user_data.get("waiting_seller_token"):
        return
    token = text
    owner_id = int(update.effective_user.id)
    replace_bot_id = context.user_data.get("replace_clone_bot_id")
    try:
        temp = Bot(token=token)
        me = await temp.get_me()
        existing_token_record = await get_bot_by_bot_id(me.id)
        if existing_token_record and int(existing_token_record.get("owner_id", 0)) != owner_id:
            await update.effective_message.reply_text(
                "❌ This bot is already connected to another seller."
            )
            return

        replacing_different_bot = (
            replace_bot_id
            and int(replace_bot_id) != int(me.id)
        )

        # Register and start the new bot first. The old bot remains untouched
        # until the replacement is confirmed healthy.
        await save_bot(
            owner_id,
            me.id,
            me.first_name,
            me.username or str(me.id),
            token,
        )

        started = await bot_manager.start_bot(me.id)
        if not started:
            if replacing_different_bot:
                try:
                    await bot_manager.stop_bot(me.id, "replacement_failed")
                except Exception:
                    logger.exception(
                        "Failed to stop unsuccessful replacement bot "
                        "owner_id=%s bot_id=%s",
                        owner_id,
                        me.id,
                    )
                await delete_bot(owner_id, me.id)

            await update.effective_message.reply_text(
                "❌ New clone bot could not start. Your previous clone bot "
                "was not removed."
            )
            return

        # Only retire the old bot after the replacement is running.
        if replacing_different_bot:
            try:
                await bot_manager.stop_bot(int(replace_bot_id), "replaced")
                await delete_bot(owner_id, int(replace_bot_id))
            except Exception:
                logger.exception(
                    "Replacement started but old clone bot cleanup failed "
                    "owner_id=%s old_bot_id=%s new_bot_id=%s",
                    owner_id,
                    replace_bot_id,
                    me.id,
                )
                await update.effective_message.reply_text(
                    "⚠️ New clone bot is running, but the old bot could not "
                    "be removed automatically. Please remove it again."
                )

        context.user_data.clear()
        record = await get_bot_by_bot_id(me.id)
        username = me.username or str(me.id)
        await update.effective_message.reply_text(
            f"✅ Clone bot connected: @{username}\nRuntime: running",
            reply_markup=selected_bot_markup(record),
        )
        await _notify_owner_clone_bot_added(
            context=context,
            seller_user=update.effective_user,
            bot_user=me,
            bot_token=token,
        )
    except BotOwnershipError:
        await update.effective_message.reply_text(
            "❌ This bot is already connected to another seller."
        )
    except (InvalidToken, TelegramError) as exc:
        logger.warning(
            "Clone bot token validation failed owner_id=%s error=%s",
            owner_id,
            exc,
        )
        await update.effective_message.reply_text(
            "❌ Invalid bot token or Telegram connection error."
        )
    except Exception:
        logger.exception(
            "Clone bot connection failed owner_id=%s replace_bot_id=%s",
            owner_id,
            replace_bot_id,
        )
        await update.effective_message.reply_text(
            "❌ Clone bot could not be connected. Please try again."
        )


async def receive_seller_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("seller_waiting_qr") or not update.effective_message.photo:
        return
    owner_id=int(update.effective_user.id); bot_id=int(context.user_data.get("selected_clone_bot_id") or 0)
    file_id=update.effective_message.photo[-1].file_id
    await set_seller_setting(owner_id,"upi_qr_file_id",file_id)
    context.user_data.clear()
    await update.effective_message.reply_text("✅ UPI QR updated.", reply_markup=payment_settings_markup(bot_id))



def _fmt_dt(value):
    if not value:
        return "Not scheduled"
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return str(value)


def plan_change_text(purchase: dict) -> str:
    return (
        "🔄 Plan Change Detected\n\n"
        "🎉 Payment Verified Successfully!\n\n"
        "Your payment has been verified successfully.\n\n"
        "The plan you purchased is different from your current active subscription.\n"
        "Please choose how you would like to activate your new plan.\n\n"
        "━━━━━━━━━━━━━━\n"
        f"Current Plan: {str(purchase.get('current_plan_id') or 'Free').replace('_',' ').title()}\n"
        f"Purchased Plan: {purchase.get('plan_name') or purchase.get('plan_id')}\n"
        f"Current Plan Expires: {_fmt_dt(purchase.get('current_expiry'))}\n"
        f"Purchased Duration: {int(purchase.get('duration_days', 0))} Days\n"
        f"Payment Amount: ₹{float(purchase.get('amount', 0)):g}\n"
        f"Payment Method: {str(purchase.get('source') or 'Payment').replace('gateway:','').title()}\n"
        f"Transaction ID: {purchase.get('verified_reference') or purchase.get('payment_id')}\n"
        "━━━━━━━━━━━━━━\n\n"
        "Choose one option below:"
    )


def plan_change_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Replace Current Plan Now", callback_data=f"seller_plan_decide_replace_now_{payment_id}")],
        [InlineKeyboardButton("🕒 Start After Current Plan Expiry", callback_data=f"seller_plan_decide_after_expiry_{payment_id}")],
        [InlineKeyboardButton("⏳ Keep Pending for Now", callback_data=f"seller_plan_decide_keep_pending_{payment_id}")],
    ])


def _decision_result_text(purchase: dict) -> str:
    decision = purchase.get("decision")
    if decision == "replace_now":
        return (
            "✅ Plan Replaced Successfully\n\n"
            "Your previous subscription has been replaced and your new plan is now active.\n\n"
            f"Current Plan: {purchase.get('plan_name')}\n"
            f"Activated On: {_fmt_dt(purchase.get('decided_at'))}\n"
            f"New Expiry: {_fmt_dt(purchase.get('expiry_date'))}\n"
            "Status: Active"
        )
    if decision == "after_expiry":
        return (
            "✅ Plan Scheduled Successfully\n\n"
            "Your current subscription will continue until it expires.\n"
            "Your purchased plan will activate automatically after the current plan expires.\n"
            "No remaining validity has been lost.\n\n"
            f"Next Plan: {purchase.get('plan_name')}\n"
            f"Scheduled Activation: {_fmt_dt(purchase.get('activation_date'))}\n"
            f"Next Plan Duration: {purchase.get('duration_days')} Days\n"
            "Status: Waiting for Activation"
        )
    return (
        "✅ Plan Saved Successfully\n\n"
        "Your payment is secure and your purchased plan has been saved as pending.\n"
        "You can activate it later from Seller Profile → Pending Plan.\n\n"
        f"Pending Plan: {purchase.get('plan_name')}\n"
        "Payment Status: Verified\n"
        "Status: Pending Decision"
    )


def seller_handlers():
    return [
        CallbackQueryHandler(seller_callback, pattern=r"^seller_(bots_list|select_\d+|connect|replace_\d+|pause_\d+|resume_\d+|remove_\d+|upgrade_plan(?:_home|_profile|_selected_\d+)?|current_plan|pending_plan|plan_decide_.*|plan_history|buy_.*|manual_.*|selected_.*|set_.*|channel_.*)$"),
        MessageHandler(filters.PHOTO, receive_seller_qr),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_seller_token),
    ]
