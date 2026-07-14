import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database.admins import is_admin
from database.payments import count_pending_payments, total_revenue
from database.seller_bots import get_bot, total_bots, set_bot_active
from database.seller_data import stats as seller_stats
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
from database.mongo import get_database


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
        [InlineKeyboardButton("🩺 Health Monitoring", callback_data="owner_health")],
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
            [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
            [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
            [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        ])
    else:
        rows.append([InlineKeyboardButton("👤 Seller Profile", callback_data="main_seller_profile")])
        rows.append([
            InlineKeyboardButton("➕ Create / Connect Clone Bot", callback_data="seller_connect")
        ])
        rows.extend([
            [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
            [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
            [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        ])

    rows.extend([
        [InlineKeyboardButton("📖 Setup Instructions", callback_data="main_child_setup")],
        [InlineKeyboardButton("🆘 Seller Help", callback_data="main_help")],
        home_button(),
    ])
    return InlineKeyboardMarkup(rows)


async def owner_dashboard_text():
    sellers = await total_sellers()
    bots = await total_bots()
    users = await total_users()
    pending = await count_pending_payments()
    revenue = await total_revenue()

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

    child_stats = await seller_stats(user_id)
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


async def list_sellers(query):
    sellers = await get_all_sellers()

    if not sellers:
        await query.edit_message_text(
            "🏪 Sellers Management\n\nNo sellers registered yet.",
            reply_markup=InlineKeyboardMarkup([home_button()]),
        )
        return

    lines = [f"🏪 Sellers Management\n\nTotal Sellers: {len(sellers)}\n"]
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

    keyboard.append([InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def seller_owner_view(query, owner_id: int):
    seller = await get_seller(owner_id)
    record = await get_bot(owner_id)

    if not seller:
        await query.edit_message_text(
            "❌ Seller not found.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Sellers", callback_data="main_owner_sellers")]
            ]),
        )
        return

    name = seller.get("first_name") or "-"
    username = f"@{seller.get('username')}" if seller.get("username") else "-"
    suspended = bool(seller.get("suspended"))

    text = (
        "🏪 Seller Details\n\n"
        f"🆔 Seller ID: {owner_id}\n"
        f"👤 Name: {name}\n"
        f"📝 Username: {username}\n"
        f"✅ Approved: {'Yes' if seller.get('approved') else 'No'}\n"
        f"🚫 Suspended: {'Yes' if suspended else 'No'}\n\n"
        f"🤖 Child Bot: @{record.get('bot_username') if record else '-'}\n"
        f"📌 Bot Status: "
        f"{'Active' if record and record.get('active') else 'Paused / Not connected'}\n"
        f"⚙ Runtime: {(record or {}).get('runtime_status','-')}"
    )

    keyboard = [
        [InlineKeyboardButton("💎 Manage Seller Subscription", callback_data="sub_mgmt_seller_control")],
        [
            InlineKeyboardButton(
                "✅ Unsuspend Seller" if suspended else "🚫 Suspend Seller",
                callback_data=(
                    f"main_seller_unsuspend_{owner_id}"
                    if suspended else f"main_seller_suspend_{owner_id}"
                ),
            )
        ],
    ]

    if record:
        if record.get("active"):
            keyboard.append([InlineKeyboardButton("⏸ Pause Clone Bot", callback_data=f"main_seller_pausebot_{owner_id}")])
        else:
            keyboard.append([InlineKeyboardButton("▶ Resume Clone Bot", callback_data=f"main_seller_resumebot_{owner_id}")])
        keyboard.append([InlineKeyboardButton("⏹ Stop Clone Bot Runtime", callback_data=f"main_seller_stopbot_{owner_id}")])

    keyboard.extend([
        [InlineKeyboardButton("⬅ Sellers", callback_data="main_owner_sellers")],
        [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
    ])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def owner_broadcast_menu(query):
    await query.edit_message_text(
        "📢 Owner Broadcast\n\nChoose audience. After choosing, send one text, photo, video, document, voice, audio, GIF, sticker or forwarded message.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏪 Sellers Only", callback_data="owner_broadcast_sellers")],
            [InlineKeyboardButton("👥 Main Bot Users (Non-Sellers)", callback_data="owner_broadcast_main_users")],
            [InlineKeyboardButton("🤖 All Clone Bot Members", callback_data="owner_broadcast_clone_users")],
            [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
        ]),
    )


async def owner_broadcast_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target=context.user_data.get("owner_broadcast_target")
    if not target or not await is_admin(update.effective_user.id):
        return
    db=get_database()
    ids=set()
    if target=="sellers":
        ids={int(x["owner_id"]) for x in await get_all_sellers() if x.get("owner_id")}
    elif target=="main_users":
        seller_ids={int(x["owner_id"]) for x in await get_all_sellers() if x.get("owner_id")}
        docs=await users_collection().find({}, {"user_id":1}).to_list(length=None)
        ids={int(x["user_id"]) for x in docs if x.get("user_id")} - seller_ids
    elif target=="clone_users":
        ids={int(x) for x in await db["seller_users"].distinct("user_id") if x}
    ids.discard(update.effective_user.id)
    sent=failed=0
    for uid in ids:
        try:
            await context.bot.copy_message(uid, update.effective_chat.id, update.effective_message.message_id)
            sent+=1
        except Exception:
            failed+=1
    context.user_data.pop("owner_broadcast_target",None)
    await update.effective_message.reply_text(
        f"✅ Broadcast completed\n\nAudience: {target.replace('_',' ').title()}\nSent: {sent}\nFailed/blocked: {failed}",
        reply_markup=owner_dashboard_keyboard(),
    )
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
                [InlineKeyboardButton("💎 Buy / Change Plan",callback_data="seller_upgrade_plan")],
                [InlineKeyboardButton("📜 Plan History",callback_data="seller_plan_history")],
                [InlineKeyboardButton("🤝 Seller Referral",callback_data="main_seller_referral")],
                [InlineKeyboardButton("🆘 Help & Commands",callback_data="main_help")],
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
        record = await get_bot(user_id)
        await query.edit_message_text(
            "📖 Clone Bot Setup Guide\n\n"
            "1. Open @BotFather\n"
            "2. Send /newbot\n"
            "3. Choose bot name and username\n"
            "4. Copy the token\n"
            "5. Return here and tap Create / Connect Clone Bot\n"
            "6. Send the token\n"
            "7. Open the new clone bot and send /admin\n\n"
            "Security: Send only your own BotFather token.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "➕ Connect Bot" if not record else "🔄 Replace Token",
                        callback_data="seller_connect" if not record else "seller_replace",
                    )
                ],
                home_button(),
            ]),
        )
        return

    if action == "main_owner_sellers":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Owner access only.")
            return
        await list_sellers(query)
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
