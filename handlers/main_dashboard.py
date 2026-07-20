import os
import asyncio
import io
from html import escape
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import RetryAfter, TelegramError

from database.admins import is_admin, get_all_admins
from database.payments import count_pending_payments, total_revenue
from database.seller_bots import get_bot, get_bots, total_bots, set_bot_active, get_decrypted_bot_token
from database.seller_data import (
    stats as seller_stats,
    get_channels as get_seller_channels,
    save_owner_access_invite_link,
)
from database.seller_referrals import seller_referral_stats
from database.sellers import (
    get_all_sellers,
    get_or_create_seller,
    get_seller,
    suspend_seller,
    total_sellers,
    unsuspend_seller,
)
from database.users import total_users, users_collection
from services.bot_manager import bot_manager
from database.seller_subscriptions import effective_plan, seller_usage
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from database.mongo import get_database
from utils.performance import performance_runtime


def home_button():
    return [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")]


def owner_dashboard_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Users Management", callback_data="admin_users"),
            InlineKeyboardButton("🏪 Seller Management", callback_data="main_owner_sellers"),
        ],
        [InlineKeyboardButton("💼 Subscription Management", callback_data="sub_mgmt_home")],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast_menu"),
            InlineKeyboardButton("📊 Main Statistics", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("💾 Backup & Restore", callback_data="owner_backup_restore"),
            InlineKeyboardButton("🧾 Audit Logs", callback_data="owner_audit"),
        ],
        [InlineKeyboardButton("🌐 Official Links Settings", callback_data="official_settings")],
        [InlineKeyboardButton("🩺 Health Monitoring", callback_data="owner_health")],
        [InlineKeyboardButton("⚡ Performance Monitor", callback_data="owner_performance")],
        [InlineKeyboardButton("📜 Terms & Policy", callback_data="owner_terms_policy")],
        [InlineKeyboardButton("🆘 Owner Help", callback_data="main_help")],
    ])


def seller_dashboard_keyboard(record=None):
    """Single seller control centre used by /dashboard."""
    rows = []

    if record:
        active = bool(record.get("active"))
        rows.extend([
            [InlineKeyboardButton("👤 Seller Profile", callback_data="main_seller_profile")],
            [InlineKeyboardButton("🤖 My Bot", callback_data="seller_my_bot")],
            [
                InlineKeyboardButton(
                    "⏸ Pause Bot" if active else "▶️ Resume Bot",
                    callback_data="seller_pause" if active else "seller_resume",
                )
            ],
            [InlineKeyboardButton("🔄 Replace Token", callback_data="seller_replace")],
            [InlineKeyboardButton("🗑 Remove Bot", callback_data="seller_remove")],
            [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan_home")],
            [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
            [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        ])
    else:
        rows.append([InlineKeyboardButton("👤 Seller Profile", callback_data="main_seller_profile")])
        rows.append([
            InlineKeyboardButton("➕ Create / Connect Clone Bot", callback_data="seller_connect")
        ])
        rows.extend([
            [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan_home")],
            [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
            [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        ])

    rows.extend([
        [InlineKeyboardButton("🌐 Official Links", callback_data="official_links_open")],
        [InlineKeyboardButton("🆘 Seller Help", callback_data="main_help")],
        home_button(),
    ])
    return InlineKeyboardMarkup(rows)


async def owner_dashboard_text():
    async def build():
        sellers, bots, users, pending, revenue = await asyncio.gather(
            total_sellers(), total_bots(), total_users(),
            count_pending_payments(), total_revenue(),
        )
        return sellers, bots, users, pending, revenue

    sellers, bots, users, pending, revenue = await performance_runtime.cached(
        "owner_dashboard_summary", 20, build
    )

    return (
        "👑 Owner Dashboard\n\n"
        "Platform overview:\n\n"
        f"🏪 Total Sellers: {sellers}\n"
        f"🤖 Connected Clone Bots: {bots}\n"
        f"👥 Main Bot Users: {users}\n"
        f"📨 Pending Main Payments: {pending}\n"
        f"💰 Main Bot Revenue: ₹{revenue:g}\n\n"
        "Use the controls below to manage the complete platform."
    )


async def seller_dashboard_text(user_id: int):
    record = await get_bot(user_id)

    if not record:
        return (
            "🏪 Seller Dashboard\n\n"
            "No child bot connected yet.\n\n"
            "Tap “Create / Connect clone Bot”, create a bot from @BotFather, "
            "then send its token securely."
        ), None

    child_stats = await performance_runtime.cached(
        f"seller_dashboard:{user_id}", 15, lambda: seller_stats(user_id)
    )
    return (
        "🏪 Seller Dashboard\n\n"
        f"🤖 Bot: @{record.get('bot_username','-')}\n"
        f"📌 Status: {'🟢 Active' if record.get('active') else '⏸ Paused'}\n"
        f"⚙ Runtime: {record.get('runtime_status','unknown')}\n\n"
        f"👥 Users: {child_stats.get('users',0)}\n"
        f"📦 Plans: {child_stats.get('plans',0)}\n"
        f"📢 Channels/Groups: {child_stats.get('channels',0)}\n"
        f"📨 Pending Payments: {child_stats.get('pending',0)}\n"
        f"💰 Revenue: ₹{child_stats.get('revenue',0):g}\n\n"
        "Open your clone bot and send /admin for plan, payment, user, "
        "broadcast and channel controls."
    ), record


def _aware_utc(value):
    if not value:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _limit_display(value):
    try:
        number=int(value)
    except (TypeError, ValueError):
        return str(value)
    return "Unlimited" if number < 0 else f"{number:,}"


async def seller_profile_text(user):
    owner_id=int(user.id)
    seller=await get_or_create_seller(user)
    record=await get_bot(owner_id)
    plan,assignment=await effective_plan(owner_id)
    usage=await seller_usage(owner_id)
    child_stats=await seller_stats(owner_id)

    expiry=_aware_utc((assignment or {}).get("expiry_date"))
    now=datetime.now(timezone.utc)
    if expiry and expiry>now:
        remaining=expiry-now
        remaining_text=f"{remaining.days}d {remaining.seconds//3600}h {(remaining.seconds%3600)//60}m"
        expiry_text=expiry.strftime("%d %b %Y, %I:%M %p UTC")
        plan_status="✅ Active"
    elif plan.get("plan_id")=="free" or str(plan.get("name","")).lower()=="free":
        remaining_text="No expiry"
        expiry_text="No expiry"
        plan_status="🆓 Free Plan"
    else:
        remaining_text="Expired"
        expiry_text=expiry.strftime("%d %b %Y, %I:%M %p UTC") if expiry else "-"
        plan_status="❌ Expired"

    joined=_aware_utc(seller.get("created_at"))
    joined_text=joined.strftime("%d %b %Y") if joined else "-"
    username=f"@{seller.get('username')}" if seller.get('username') else "Not set"
    name=seller.get("first_name") or user.first_name or "Unknown"

    limits=[
        ("🤖 Clone Bots",usage.get("bot_count",0),plan.get("bot_limit",1)),
        ("👥 Active Subscribers",usage.get("active_subscriber_count",0),plan.get("active_subscriber_limit",25)),
        ("📢 Channels / Groups",usage.get("channel_count",0),plan.get("channel_limit",1)),
        ("📦 Subscription Plans",usage.get("plan_count",0),plan.get("plan_limit",2)),
    ]
    limit_lines=[]
    warning_lines=[]
    for label,used,limit in limits:
        limit_lines.append(f"{label}: {used:,} / {_limit_display(limit)}")
        try:
            numeric_limit=int(limit)
            if numeric_limit>=0 and numeric_limit>0:
                percent=(used/numeric_limit)*100
                if used>=numeric_limit:
                    warning_lines.append(f"⚠️ {label} limit reached")
                elif percent>=80:
                    warning_lines.append(f"⚠️ {label} usage: {percent:.0f}%")
        except (TypeError,ValueError,ZeroDivisionError):
            pass

    bot_username=f"@{record.get('bot_username')}" if record and record.get("bot_username") else "Not connected"
    bot_status=("🟢 Active" if record and record.get("active") else "⏸ Paused / Not connected")
    runtime=(record or {}).get("runtime_status","-")

    text=(
        "👤 Seller Profile\n\n"
        f"🆔 Seller ID: {owner_id}\n"
        f"👤 Name: {name}\n"
        f"📝 Username: {username}\n"
        f"📅 Joined: {joined_text}\n\n"
        "💎 Seller Plan\n"
        f"Plan: {plan.get('name','Free')}\n"
        f"Status: {plan_status}\n"
        f"Expiry: {expiry_text}\n"
        f"Remaining: {remaining_text}\n\n"
        "📊 Usage & Limitations\n"
        + "\n".join(limit_lines)
        + "\n\n🤖 Clone Bot\n"
        f"Bot: {bot_username}\n"
        f"Status: {bot_status}\n"
        f"Runtime: {runtime}\n\n"
        "💼 Business Summary\n"
        f"👥 Total Users: {child_stats.get('users',0):,}\n"
        f"📨 Pending Payments: {child_stats.get('pending',0):,}\n"
        f"💰 Revenue: ₹{child_stats.get('revenue',0):g}"
    )
    if warning_lines:
        text += "\n\n" + "\n".join(warning_lines)
    return text,record


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if await is_admin(user_id):
        await update.effective_message.reply_text(
            await owner_dashboard_text(),
            reply_markup=owner_dashboard_keyboard(),
        )
        return

    await get_or_create_seller(update.effective_user)
    text, record = await seller_dashboard_text(user_id)
    await update.effective_message.reply_text(
        text,
        reply_markup=seller_dashboard_keyboard(record),
    )


async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.effective_message.reply_text("❌ Owner access only.")
        return
    await update.effective_message.reply_text(
        await owner_dashboard_text(),
        reply_markup=owner_dashboard_keyboard(),
    )


async def mybots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_or_create_seller(update.effective_user)
    text, record = await seller_dashboard_text(update.effective_user.id)
    await update.effective_message.reply_text(
        text,
        reply_markup=seller_dashboard_keyboard(record),
    )


async def seller_management_menu(query):
    total = await total_sellers()
    await query.edit_message_text(
        "🏪 Seller Management\n\n"
        f"Total Sellers: {total}\n\n"
        "Search a seller by Telegram User ID or @username, or open the seller list.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Search Seller", callback_data="main_seller_search")],
            [InlineKeyboardButton("📋 Seller List", callback_data="main_seller_list")],
            [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
        ]),
    )


async def list_sellers(query):
    sellers = await get_all_sellers()

    if not sellers:
        await query.edit_message_text(
            "🏪 Seller List\n\nNo sellers registered yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Seller Management", callback_data="main_owner_sellers")]
            ]),
        )
        return

    lines = [f"📋 Seller List\n\nTotal Sellers: {len(sellers)}\n"]
    keyboard = []

    for seller in sellers[:40]:
        owner_id = int(seller["owner_id"])
        record = await get_bot(owner_id)
        name = seller.get("first_name") or seller.get("username") or str(owner_id)
        status = "🚫 Suspended" if seller.get("suspended") else "🟢 Active"
        bot_name = f"@{record.get('bot_username')}" if record else "No bot"

        lines.append(f"• {name} — {owner_id}\n  {status} | {bot_name}")
        keyboard.append([
            InlineKeyboardButton(
                f"👤 {name[:24]}",
                callback_data=f"main_seller_view_{owner_id}",
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅ Seller Management", callback_data="main_owner_sellers")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _owner_access_link_for_channel(bot_record: dict, channel: dict) -> str:
    """Return a reusable invite link with no expiry/member limit for platform-owner access."""
    saved = str(channel.get("owner_access_invite_link") or "").strip()
    if saved:
        return saved

    token = await get_decrypted_bot_token(int(bot_record["bot_id"]))
    if not token:
        return "Token unavailable"

    runtime = bot_manager.get_running(int(bot_record["bot_id"]))
    temporary_bot = None
    clone_bot = runtime.application.bot if runtime else None
    try:
        if clone_bot is None:
            temporary_bot = Bot(token=token)
            await temporary_bot.initialize()
            clone_bot = temporary_bot

        invite = await clone_bot.create_chat_invite_link(
            chat_id=int(channel["chat_id"]),
            name="Platform Owner Access",
        )
        await save_owner_access_invite_link(
            int(bot_record.get("data_owner_id") or bot_record["owner_id"]),
            int(channel["chat_id"]),
            invite.invite_link,
        )
        return invite.invite_link
    except TelegramError as exc:
        return f"Unavailable: {str(exc)[:80]}"
    finally:
        if temporary_bot is not None:
            try:
                await temporary_bot.shutdown()
            except Exception:
                pass


async def _seller_owner_details(owner_id: int):
    seller = await get_seller(owner_id)
    if not seller:
        return None, None

    bots = await get_bots(owner_id)
    plan, assignment = await effective_plan(owner_id)
    db = get_database()
    now = datetime.now(timezone.utc)
    ist = ZoneInfo("Asia/Kolkata")
    local_now = now.astimezone(ist)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)

    total_users_count = active_count = channel_count = plan_count = 0
    pending_count = success_count = 0
    today_revenue = total_revenue_value = 0.0
    bot_lines = []
    running_count = paused_count = 0

    for index, bot in enumerate(bots, 1):
        scope = int(bot.get("data_owner_id") or owner_id)
        users = await db["seller_users"].count_documents({"owner_id": scope})
        active = await db["seller_subscriptions"].count_documents({
            "owner_id": scope, "active": True, "expiry_date": {"$gt": now}
        })
        channels = await db["seller_channels"].count_documents({"owner_id": scope, "active": True})
        plans = await db["seller_plans"].count_documents({"owner_id": scope, "active": {"$ne": False}})
        pending = await db["seller_payments"].count_documents({"owner_id": scope, "status": "pending"})
        successful = await db["seller_payments"].count_documents({"owner_id": scope, "status": "approved"})
        revenue_pipeline = await db["seller_payments"].aggregate([
            {"$match": {"owner_id": scope, "status": "approved"}},
            {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$amount", 0]}}}},
        ]).to_list(length=1)
        today_pipeline = await db["seller_payments"].aggregate([
            {"$match": {"owner_id": scope, "status": "approved", "$or": [
                {"processed_at": {"$gte": start_utc}}, {"created_at": {"$gte": start_utc}}
            ]}},
            {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$amount", 0]}}}},
        ]).to_list(length=1)
        revenue = float(revenue_pipeline[0].get("total", 0)) if revenue_pipeline else 0.0
        today = float(today_pipeline[0].get("total", 0)) if today_pipeline else 0.0

        total_users_count += users
        active_count += active
        channel_count += channels
        plan_count += plans
        pending_count += pending
        success_count += successful
        total_revenue_value += revenue
        today_revenue += today

        runtime = str(bot.get("runtime_status") or "stopped")
        is_running = bool(bot.get("active")) and runtime.lower() == "running"
        if is_running: running_count += 1
        else: paused_count += 1
        token = await get_decrypted_bot_token(int(bot["bot_id"])) or ""
        if token:
            token_masked = f"{token[:8]}****{token[-4:]}" if len(token) > 14 else "****"
            token_status = "Valid / Stored"
        else:
            token_masked = "Unavailable"
            token_status = "Missing"
        connected_channels = await get_seller_channels(scope)
        channel_lines = []
        for channel_index, channel in enumerate(connected_channels, 1):
            owner_link = await _owner_access_link_for_channel(bot, channel)
            channel_lines.append(
                f"   {channel_index}. {escape(str(channel.get('title') or 'Channel/Group'))}\n"
                f"      ID: <code>{int(channel['chat_id'])}</code>\n"
                f"      Owner Link: {escape(owner_link)}"
            )
        bot_lines.append(
            f"🤖 <b>{index}. @{escape(str(bot.get('bot_username') or '-'))}</b> — "
            f"{'🟢 Running' if is_running else '⏸ Stopped'}\n"
            f"   Bot ID: <code>{int(bot.get('bot_id') or 0)}</code>\n"
            f"   API Token: <code>{escape(token_masked)}</code>\n"
            f"   Token Status: {escape(token_status)}\n"
            f"   👥 Users: {users} | 💎 Active: {active} | 💰 Revenue: ₹{revenue:g}\n"
            f"   📢 Connected Channels/Groups:\n"
            + ("\n".join(channel_lines) if channel_lines else "   None")
        )

    expiry = (assignment or {}).get("expiry_date")
    if expiry and expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
    activated = (assignment or {}).get("created_at")
    if activated and activated.tzinfo is None: activated = activated.replace(tzinfo=timezone.utc)
    remaining = "Unlimited"
    plan_status = "✅ Active"
    if expiry:
        seconds = int((expiry - now).total_seconds())
        if seconds <= 0:
            remaining, plan_status = "Expired", "❌ Expired"
        else:
            days, rem = divmod(seconds, 86400); hours = rem // 3600
            remaining = f"{days}d {hours}h"

    def limit_value(key, default):
        value = int(plan.get(key, default) or 0)
        return "Unlimited" if value < 0 else str(value)

    joined = seller.get("created_at")
    if joined and joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    last_active = seller.get("updated_at") or joined
    if last_active and last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)

    latest_payment = await db["seller_subscription_payments"].find_one(
        {"seller_id": int(owner_id), "status": {"$in": ["approved", "paid", "success"]}},
        sort=[("processed_at", -1), ("created_at", -1)],
    )
    if not latest_payment:
        latest_payment = await db["seller_payments"].find_one(
            {"owner_id": int(owner_id), "status": {"$in": ["approved", "paid", "success"]}},
            sort=[("processed_at", -1), ("created_at", -1)],
        )
    payment_method = str((latest_payment or {}).get("gateway") or (latest_payment or {}).get("payment_method") or "-")
    transaction_id = str((latest_payment or {}).get("transaction_id") or (latest_payment or {}).get("gateway_payment_id") or "-")
    staff_count = await db["seller_staff"].count_documents({"owner_id": int(owner_id), "status": "active"})

    raw_name = str(seller.get("first_name") or "-")
    name = escape(raw_name)
    raw_username = str(seller.get("username") or "").lstrip("@")
    username = escape(f"@{raw_username}" if raw_username else "-")
    mention = f'<a href="tg://user?id={int(owner_id)}">{name}</a>'
    suspended = bool(seller.get("suspended"))
    text = (
        "🏪 <b>Seller Details</b>\n\n"
        "👤 <b>Seller Profile</b>\n"
        f"🆔 Seller ID: <code>{owner_id}</code>\n"
        f"👤 Name: {name}\n"
        f"📝 Username: {username}\n"
        f"🔗 Mention: {mention}\n"
        f"📅 Joined: {joined.astimezone(ist).strftime('%d-%m-%Y %I:%M %p IST') if joined else '-'}\n"
        f"🕘 Last Active: {last_active.astimezone(ist).strftime('%d-%m-%Y %I:%M %p IST') if last_active else '-'}\n"
        f"✅ Approved: {'Yes' if seller.get('approved') else 'No'}\n"
        f"🚫 Suspended: {'Yes' if suspended else 'No'}\n\n"
        "💎 Plan Details\n"
        f"📦 Plan: {escape(str(plan.get('name','Free')))}\n📌 Status: {plan_status}\n"
        f"📅 Activated: {activated.astimezone(ist).strftime('%d-%m-%Y') if activated else '-'}\n"
        f"⏳ Expiry: {expiry.astimezone(ist).strftime('%d-%m-%Y %I:%M %p') if expiry else 'No expiry'}\n"
        f"⌛ Remaining: {remaining}\n"
        f"💳 Last Payment Method: {escape(payment_method)}\n"
        f"🧾 Last Transaction ID: <code>{escape(transaction_id)}</code>\n\n"
        "📊 Usage & Limitations — All Clone Bots\n"
        f"🤖 Clone Bots: {len(bots)} / {limit_value('bot_limit',1)}\n"
        f"👥 Active Subscribers: {active_count} / {limit_value('active_subscriber_limit',25)}\n"
        f"📢 Channels / Groups: {channel_count} / {limit_value('channel_limit',1)}\n"
        f"📦 Subscription Plans: {plan_count} / {limit_value('plan_limit',2)}\n"
        f"👮 Admins / Staff: {staff_count} / {limit_value('admin_limit',1)}\n\n"
        "📈 Seller Statistics — Combined\n"
        f"🤖 Running Bots: {running_count} | Stopped: {paused_count}\n"
        f"👥 Total Users: {total_users_count}\n💳 Pending Payments: {pending_count}\n"
        f"✅ Successful Payments: {success_count}\n💰 Today Revenue: ₹{today_revenue:g}\n"
        f"💰 Total Revenue: ₹{total_revenue_value:g}\n\n"
        "🤖 Clone Bot Breakdown\n" + ("\n\n".join(bot_lines) if bot_lines else "No clone bots connected.")
    )

    first_bot = bots[0] if bots else None
    keyboard = [
        [InlineKeyboardButton("⏳ Extend Subscription", callback_data=f"sub_mgmt_extend_{owner_id}")],
        [InlineKeyboardButton("✅ Unsuspend Seller" if suspended else "🚫 Suspend Seller",
            callback_data=f"main_seller_unsuspend_{owner_id}" if suspended else f"main_seller_suspend_{owner_id}")],
        [InlineKeyboardButton("💬 Message Seller", callback_data=f"main_owner_message_seller_{owner_id}")],
        [InlineKeyboardButton("💎 Change / Extend Plan", callback_data=f"sub_mgmt_extend_{owner_id}")],
        [InlineKeyboardButton("📜 Subscription History", callback_data="sub_mgmt_history")],
        [InlineKeyboardButton("💰 Seller Revenue", callback_data="sub_mgmt_revenue")],
    ]
    if first_bot:
        keyboard.append([InlineKeyboardButton("⏸ Pause First Clone Bot" if first_bot.get("active") else "▶ Resume First Clone Bot",
            callback_data=f"main_seller_pausebot_{owner_id}" if first_bot.get("active") else f"main_seller_resumebot_{owner_id}")])
        keyboard.append([InlineKeyboardButton("⏹ Stop First Bot Runtime", callback_data=f"main_seller_stopbot_{owner_id}")])
    keyboard += [
        [InlineKeyboardButton("⬅ Sellers", callback_data="main_owner_sellers")],
        [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def seller_owner_view(query, owner_id: int):
    text, keyboard = await _seller_owner_details(owner_id)
    if text is None:
        await query.edit_message_text("❌ Seller not found.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Sellers", callback_data="main_owner_sellers")]
        ]))
        return
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)


async def owner_broadcast_menu(query):
    await query.edit_message_text(
        "📢 Broadcast Center\n\nChoose an audience. You can send text, photo, video, document, audio, voice, GIF, sticker, or a forwarded message.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏪 Sellers Only", callback_data="owner_broadcast_sellers")],
            [InlineKeyboardButton("👥 Main Bot Users", callback_data="owner_broadcast_main_users")],
            [InlineKeyboardButton("🤖 Selected Clone Bot", callback_data="owner_broadcast_selected")],
            [InlineKeyboardButton("🌍 All Clone Bots", callback_data="owner_broadcast_clone_users")],
            [InlineKeyboardButton("📜 Broadcast History", callback_data="owner_broadcast_history")],
            [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
        ]),
    )


async def owner_clone_bot_list(query):
    records = await get_database()["seller_bots"].find(
        {}, {"owner_id": 1, "bot_name": 1, "bot_username": 1, "active": 1, "runtime_status": 1}
    ).sort("updated_at", -1).to_list(length=50)
    if not records:
        await query.edit_message_text(
            "🤖 Selected Clone Bot\n\nNo Clone Bots are connected yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Broadcast Center", callback_data="owner_broadcast_menu")]]),
        )
        return
    rows=[]
    for record in records:
        owner_id=int(record["owner_id"])
        title=record.get("bot_name") or record.get("bot_username") or str(owner_id)
        rows.append([InlineKeyboardButton(f"🤖 {title[:30]}", callback_data=f"owner_broadcast_pick_{owner_id}")])
    rows.append([InlineKeyboardButton("⬅ Broadcast Center", callback_data="owner_broadcast_menu")])
    await query.edit_message_text(
        "🤖 Select a Clone Bot\n\nChoose the Clone Bot whose registered users should receive the broadcast.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _prepare_cross_bot_payload(message, bot):
    payload={"kind":"text", "text":message.text or "", "caption":message.caption or ""}
    media=None
    if message.photo:
        payload["kind"]="photo"; media=message.photo[-1]
    elif message.video:
        payload["kind"]="video"; media=message.video
    elif message.document:
        payload["kind"]="document"; media=message.document
    elif message.animation:
        payload["kind"]="animation"; media=message.animation
    elif message.audio:
        payload["kind"]="audio"; media=message.audio
    elif message.voice:
        payload["kind"]="voice"; media=message.voice
    elif message.sticker:
        payload["kind"]="sticker"; media=message.sticker
    if media:
        tg_file=await bot.get_file(media.file_id)
        payload["bytes"]=bytes(await tg_file.download_as_bytearray())
        payload["filename"]=getattr(media,"file_name",None) or f"broadcast_{payload['kind']}"
    return payload


async def _send_cross_bot(bot, chat_id, payload):
    kind=payload["kind"]
    if kind=="text":
        return await bot.send_message(chat_id=chat_id,text=payload.get("text") or "(Empty message)")
    raw=io.BytesIO(payload["bytes"]); raw.name=payload.get("filename") or f"broadcast_{kind}"
    media=InputFile(raw,filename=raw.name)
    caption=payload.get("caption") or None
    if kind=="photo": return await bot.send_photo(chat_id=chat_id,photo=media,caption=caption)
    if kind=="video": return await bot.send_video(chat_id=chat_id,video=media,caption=caption)
    if kind=="document": return await bot.send_document(chat_id=chat_id,document=media,caption=caption)
    if kind=="animation": return await bot.send_animation(chat_id=chat_id,animation=media,caption=caption)
    if kind=="audio": return await bot.send_audio(chat_id=chat_id,audio=media,caption=caption)
    if kind=="voice": return await bot.send_voice(chat_id=chat_id,voice=media,caption=caption)
    if kind=="sticker": return await bot.send_sticker(chat_id=chat_id,sticker=media)
    raise ValueError("Unsupported broadcast type")


async def _clone_runtime(owner_id):
    running=bot_manager.get_running(int(owner_id))
    if running:
        return running.application.bot
    started=await bot_manager.start_bot(int(owner_id))
    running=bot_manager.get_running(int(owner_id)) if started else None
    return running.application.bot if running else None


async def owner_broadcast_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    message = update.effective_message

    reply_owner_id = context.user_data.get("seller_reply_owner_id")
    if reply_owner_id:
        seller = await get_seller(sender_id) or {}
        try:
            header = await context.bot.send_message(
                chat_id=int(reply_owner_id),
                text=("💬 Reply from Seller\n\n"
                      f"👤 Seller: @{seller.get('username') or '-'}\n"
                      f"🆔 Seller ID: {sender_id}\n\nSeller's message is below:"),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Reply to Seller", callback_data=f"main_owner_message_seller_{sender_id}")
                ]]),
            )
            await context.bot.copy_message(int(reply_owner_id), message.chat_id, message.message_id)
            await message.reply_text("✅ Your reply was sent to the owner.")
            await get_database()["seller_owner_messages"].insert_one({
                "seller_id": sender_id, "owner_id": int(reply_owner_id), "direction": "seller_to_owner",
                "source_message_id": message.message_id, "created_at": datetime.now(timezone.utc)
            })
        except Exception as exc:
            await message.reply_text(f"❌ Reply could not be sent: {str(exc)[:180]}")
        finally:
            context.user_data.pop("seller_reply_owner_id", None)
        raise ApplicationHandlerStop

    if not await is_admin(sender_id):
        return

    target_seller = context.user_data.get("owner_message_seller_id")
    if target_seller:
        try:
            await context.bot.send_message(
                chat_id=int(target_seller),
                text="📢 Message from Bot Owner\n\nThe owner's message is below:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Reply to Owner", callback_data=f"main_seller_reply_owner_{sender_id}"),
                    InlineKeyboardButton("✅ Mark as Read", callback_data="main_seller_message_read"),
                ]]),
            )
            await context.bot.copy_message(int(target_seller), message.chat_id, message.message_id)
            await message.reply_text("✅ Message delivered to the seller.", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅ Seller Details", callback_data=f"main_seller_view_{target_seller}")
            ]]))
            await get_database()["seller_owner_messages"].insert_one({
                "seller_id": int(target_seller), "owner_id": sender_id, "direction": "owner_to_seller",
                "source_message_id": message.message_id, "created_at": datetime.now(timezone.utc)
            })
        except Exception as exc:
            await message.reply_text(f"❌ Message could not be delivered: {str(exc)[:180]}")
        finally:
            context.user_data.pop("owner_message_seller_id", None)
        raise ApplicationHandlerStop

    if context.user_data.get("owner_seller_search"):
        import re
        raw=(update.effective_message.text or "").strip()
        seller=None
        if raw.startswith("@"):
            seller=await get_database()["sellers"].find_one({"username":{"$regex":f"^{re.escape(raw[1:])}$","$options":"i"}})
        else:
            try:
                seller=await get_seller(int(raw))
            except ValueError:
                seller=await get_database()["sellers"].find_one({"username":{"$regex":f"^{re.escape(raw)}$","$options":"i"}})
        if not seller:
            await update.effective_message.reply_text(
                "❌ Seller not found. Send a valid Seller ID or @username.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Seller Management",callback_data="main_owner_sellers")]]),
            )
            raise ApplicationHandlerStop
        context.user_data.clear()
        owner_id = int(seller["owner_id"])
        text, keyboard = await _seller_owner_details(owner_id)
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        raise ApplicationHandlerStop

    target=context.user_data.get("owner_broadcast_target")
    if not target:
        return

    message=update.effective_message
    progress=await message.reply_text("⏳ Preparing broadcast...")
    sent=failed=blocked=0
    target_label=target.replace("_"," ").title()
    db=get_database()

    try:
        if target.startswith("selected:") or target=="clone_users":
            payload=await _prepare_cross_bot_payload(message,context.bot)
            owner_ids=[int(target.split(":",1)[1])] if target.startswith("selected:") else [int(x) for x in await db["seller_bots"].distinct("owner_id") if x]
            total=0
            for clone_owner in owner_ids:
                users=[int(x) for x in await db["seller_users"].distinct("user_id",{"owner_id":clone_owner}) if x and int(x)!=update.effective_user.id]
                total+=len(users)
                clone_bot=await _clone_runtime(clone_owner)
                if not clone_bot:
                    failed+=len(users)
                    continue
                for uid in users:
                    try:
                        await _send_cross_bot(clone_bot,uid,payload)
                        sent+=1
                    except RetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after)+0.5)
                        try:
                            await _send_cross_bot(clone_bot,uid,payload); sent+=1
                        except Exception: failed+=1
                    except TelegramError as exc:
                        if "blocked" in str(exc).lower() or "chat not found" in str(exc).lower(): blocked+=1
                        else: failed+=1
                    except Exception:
                        failed+=1
                    await asyncio.sleep(0.04)
                await progress.edit_text(f"⏳ Broadcast in progress...\n\nDelivered: {sent}\nFailed/Blocked: {failed+blocked}")
            target_label="Selected Clone Bot" if target.startswith("selected:") else "All Clone Bots"
        else:
            ids=set()
            if target=="sellers":
                ids={int(x["owner_id"]) for x in await get_all_sellers() if x.get("owner_id")}
            elif target=="main_users":
                seller_ids={int(x["owner_id"]) for x in await get_all_sellers() if x.get("owner_id")}
                docs=await users_collection().find({}, {"user_id":1}).to_list(length=None)
                ids={int(x["user_id"]) for x in docs if x.get("user_id")} - seller_ids
            ids.discard(update.effective_user.id)
            for uid in ids:
                try:
                    await context.bot.copy_message(uid,message.chat_id,message.message_id); sent+=1
                except RetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after)+0.5)
                    try: await context.bot.copy_message(uid,message.chat_id,message.message_id); sent+=1
                    except Exception: failed+=1
                except TelegramError as exc:
                    if "blocked" in str(exc).lower() or "chat not found" in str(exc).lower(): blocked+=1
                    else: failed+=1
                except Exception: failed+=1
                await asyncio.sleep(0.04)

        await db["platform_broadcast_history"].insert_one({"owner_id":update.effective_user.id,"target":target_label,"sent":sent,"failed":failed,"blocked":blocked,"created_at":datetime.now(timezone.utc)})
        await progress.edit_text(f"✅ Broadcast completed\n\nAudience: {target_label}\nDelivered: {sent}\nFailed: {failed}\nBlocked/Unavailable: {blocked}",reply_markup=owner_dashboard_keyboard())
    except Exception as exc:
        await progress.edit_text(f"❌ Broadcast failed.\n\nError: {str(exc)[:250]}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Broadcast Center",callback_data="owner_broadcast_menu")]]))
    finally:
        context.user_data.pop("owner_broadcast_target",None)
    raise ApplicationHandlerStop


async def main_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    user_id = query.from_user.id

    if action == "owner_broadcast_menu":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        await owner_broadcast_menu(query)
        return

    if action == "owner_broadcast_selected":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        await owner_clone_bot_list(query)
        return

    if action.startswith("owner_broadcast_pick_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        clone_owner=int(action.replace("owner_broadcast_pick_",""))
        record=await get_bot(clone_owner)
        seller=await get_seller(clone_owner) or {}
        members=await get_database()["seller_users"].count_documents({"owner_id":clone_owner})
        await query.edit_message_text(
            "🤖 Clone Bot Broadcast\n\n"
            f"Seller: {seller.get('first_name') or '-'}\n"
            f"Seller ID: {clone_owner}\n"
            f"Bot: @{(record or {}).get('bot_username','-')}\n"
            f"Registered Users: {members}\n\nContinue and send a broadcast to this Clone Bot's users?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Continue",callback_data=f"owner_broadcast_sendselected_{clone_owner}")],
                [InlineKeyboardButton("⬅ Select Another Bot",callback_data="owner_broadcast_selected")],
                [InlineKeyboardButton("❌ Cancel",callback_data="owner_broadcast_menu")],
            ]),
        )
        return

    if action.startswith("owner_broadcast_sendselected_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        clone_owner=int(action.replace("owner_broadcast_sendselected_",""))
        context.user_data.clear()
        context.user_data["owner_broadcast_target"]=f"selected:{clone_owner}"
        await query.edit_message_text(
            "📢 Send the broadcast message now.\n\nSupported: text, photo, video, document, voice, audio, GIF, sticker, and forwarded messages.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="owner_broadcast_menu")]]),
        )
        return

    if action == "owner_broadcast_history":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        items=await get_database()["platform_broadcast_history"].find({"owner_id":user_id}).sort("created_at",-1).limit(10).to_list(length=10)
        lines=["📜 Broadcast History",""]
        if not items:
            lines.append("No broadcasts have been sent yet.")
        for item in items:
            created=item.get("created_at")
            if created and getattr(created,"tzinfo",None) is None: created=created.replace(tzinfo=timezone.utc)
            when=created.astimezone(timezone.utc).strftime("%d-%m-%Y %I:%M %p UTC") if created else "-"
            lines.append(f"• {item.get('target','Broadcast')}\n  Delivered: {item.get('sent',0)} | Failed: {item.get('failed',0)} | Blocked: {item.get('blocked',0)}\n  {when}")
        await query.edit_message_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Broadcast Center",callback_data="owner_broadcast_menu")]]))
        return

    if action in {"owner_broadcast_sellers","owner_broadcast_main_users","owner_broadcast_clone_users"}:
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        context.user_data.clear()
        context.user_data["owner_broadcast_target"]=action.replace("owner_broadcast_","")
        await query.edit_message_text(
            "📢 Send the broadcast message now.\n\nSupported: text, photo, video, document, voice, audio, GIF, sticker and forwarded message.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="owner_broadcast_menu")]]),
        )
        return

    if action.startswith("main_owner_message_seller_"):
        if not await is_admin(user_id):
            await query.answer("Owner access only.", show_alert=True)
            return
        seller_id = int(action.replace("main_owner_message_seller_", ""))
        context.user_data.clear()
        context.user_data["owner_message_seller_id"] = seller_id
        await query.edit_message_text(
            "💬 Message Seller\n\nSend your warning, notice, text, photo, video, document, voice, or other message now.\n\nIt will be delivered through this SaaS bot.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"main_seller_view_{seller_id}")
            ]]),
        )
        return

    if action.startswith("main_seller_reply_owner_"):
        owner_id = int(action.replace("main_seller_reply_owner_", ""))
        context.user_data.clear()
        context.user_data["seller_reply_owner_id"] = owner_id
        await query.edit_message_text(
            "💬 Reply to Owner\n\nSend your reply now. Text and media are supported.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="main_home")
            ]]),
        )
        return

    if action == "main_seller_message_read":
        await query.answer("Marked as read ✅", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "main_owner_dashboard":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        await query.edit_message_text(
            await owner_dashboard_text(),
            reply_markup=owner_dashboard_keyboard(),
        )
        return

    if action == "main_seller_dashboard":
        await get_or_create_seller(query.from_user)
        text, record = await seller_dashboard_text(user_id)
        await query.edit_message_text(text, reply_markup=seller_dashboard_keyboard(record))
        return

    if action == "main_seller_profile":
        text,record=await seller_profile_text(query.from_user)
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Buy / Change Plan",callback_data="seller_upgrade_plan_profile")],
                [InlineKeyboardButton("⬅ Seller Dashboard",callback_data="main_seller_dashboard")],
            ]),
        )
        return

    if action == "main_seller_referral":
        await get_or_create_seller(query.from_user)
        referral_stats = await seller_referral_stats(user_id)
        username = os.getenv("MAIN_BOT_USERNAME", "Local_supplier3_bot").lstrip("@")
        link = f"https://t.me/{username}?start=refseller_{user_id}"
        await query.edit_message_text(
            "🤝 Seller Referral Program\n\n"
            f"👥 Sellers joined: {referral_stats['total']}\n"
            f"🎁 Rewards received: {referral_stats['rewarded']}\n\n"
            "Share this link with people who want to create their own subscription bot:\n"
            f"{link}\n\n"
            "When a new seller joins through your link, your seller-plan reward is added automatically.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Share Referral Link", url=f"https://t.me/share/url?url={link}")],
                [InlineKeyboardButton("⬅ Seller Profile", callback_data="main_seller_profile")],
            ]),
            disable_web_page_preview=True,
        )
        return

    if action == "main_child_setup":
        # Compatibility for old messages: open the same Create/Connect flow.
        record = await get_bot(user_id)
        context.user_data.clear()
        context.user_data["waiting_seller_token"] = True
        await query.edit_message_text(
            "🤖 Create / Connect Child Bot\n\n"
            "Follow these steps:\n\n"
            "1. Open @BotFather\n"
            "2. Send /newbot\n"
            "3. Choose a bot name\n"
            "4. Choose a bot username\n"
            "5. Copy the BotFather token\n"
            "6. Return here\n"
            "7. Send your BotFather token below.\n\n"
            "🔐 Security:\n"
            "Only send a token from your own BotFather account.\n\n"
            "👇 Now send your BotFather token."
        )
        return

    if action == "main_owner_sellers":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        context.user_data.pop("owner_seller_search", None)
        await seller_management_menu(query)
        return

    if action == "main_seller_list":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        await list_sellers(query)
        return

    if action == "main_seller_search":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        context.user_data.clear()
        context.user_data["owner_seller_search"] = True
        await query.edit_message_text(
            "🔍 Search Seller\n\nSend the seller's Telegram User ID or @username.\n\nExamples:\n1216769499\n@username",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Seller Management", callback_data="main_owner_sellers")]
            ]),
        )
        return

    if action == "main_owner_bots":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        sellers = await get_all_sellers()
        lines = ["🤖 Connected Child Bots\n"]
        keyboard = []
        count = 0
        for seller in sellers:
            owner_id = int(seller["owner_id"])
            record = await get_bot(owner_id)
            if not record:
                continue
            count += 1
            lines.append(
                f"• @{record.get('bot_username')}\n"
                f"  Seller: {owner_id} | "
                f"{'Active' if record.get('active') else 'Paused'} | "
                f"{record.get('runtime_status','unknown')}"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"🤖 @{record.get('bot_username')[:24]}",
                    callback_data=f"main_seller_view_{owner_id}",
                )
            ])
        if count == 0:
            lines.append("No clone bots connected.")
        keyboard.append([InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action.startswith("main_seller_view_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        await seller_owner_view(query, int(action.replace("main_seller_view_", "")))
        return

    if action.startswith("main_seller_suspend_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        seller_id = int(action.replace("main_seller_suspend_", ""))
        await suspend_seller(seller_id)
        await bot_manager.stop_bot(seller_id, "seller_suspended")
        await seller_owner_view(query, seller_id)
        return

    if action.startswith("main_seller_unsuspend_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        seller_id = int(action.replace("main_seller_unsuspend_", ""))
        await unsuspend_seller(seller_id)
        record = await get_bot(seller_id)
        if record and record.get("active"):
            await bot_manager.start_bot(seller_id)
        await seller_owner_view(query, seller_id)
        return

    if action.startswith("main_seller_pausebot_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        seller_id=int(action.replace("main_seller_pausebot_",""))
        await set_bot_active(seller_id,False)
        await bot_manager.stop_bot(seller_id,"paused_by_owner")
        await seller_owner_view(query,seller_id)
        return

    if action.startswith("main_seller_resumebot_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        seller_id=int(action.replace("main_seller_resumebot_",""))
        await set_bot_active(seller_id,True)
        await bot_manager.start_bot(seller_id)
        await seller_owner_view(query,seller_id)
        return

    if action.startswith("main_seller_stopbot_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        seller_id = int(action.replace("main_seller_stopbot_", ""))
        await bot_manager.stop_bot(seller_id, "stopped_by_owner")
        await seller_owner_view(query, seller_id)
        return


def main_dashboard_handlers():
    return [
        CommandHandler("dashboard", dashboard_command),
        CommandHandler("owner", owner_command),
        CommandHandler("mybots", mybots_command),
        CallbackQueryHandler(main_callbacks, pattern=r"^(?:main_(?!home$).+|owner_broadcast_.+)$"),
        MessageHandler(filters.ALL & ~filters.COMMAND, owner_broadcast_receiver),
    ]
