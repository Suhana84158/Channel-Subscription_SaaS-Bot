from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database.admins import is_admin
from database.payments import count_pending_payments, total_revenue
from database.seller_bots import get_bot, total_bots
from database.seller_data import stats as seller_stats
from database.sellers import (
    get_all_sellers,
    get_or_create_seller,
    get_seller,
    suspend_seller,
    total_sellers,
    unsuspend_seller,
)
from database.users import total_users
from services.bot_manager import bot_manager


def home_button():
    return [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")]


def owner_dashboard_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Users Management", callback_data="admin_users"),
            InlineKeyboardButton("🏪 Sellers Management", callback_data="main_owner_sellers"),
        ],
        [
            InlineKeyboardButton("🤖 Child Bots", callback_data="main_owner_bots"),
            InlineKeyboardButton("💳 Pending Payments", callback_data="admin_pending_payments"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Main Statistics", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("📋 Channels / Groups", callback_data="admin_channels"),
            InlineKeyboardButton("⚙ Settings", callback_data="admin_bot_settings"),
        ],
        [InlineKeyboardButton("💼 Subscription Management", callback_data="sub_mgmt_home")],
        [
            InlineKeyboardButton("🧰 Seller Management+", callback_data="owner_seller_management_plus"),
            InlineKeyboardButton("🩺 Health Monitoring", callback_data="owner_health"),
        ],
        [
            InlineKeyboardButton("💾 Backup & Export", callback_data="owner_backup_export"),
            InlineKeyboardButton("🧾 Audit Logs", callback_data="owner_audit"),
        ],
        [InlineKeyboardButton("📜 Terms & Policy", callback_data="owner_terms_policy")],
        [InlineKeyboardButton("🆘 Owner Help", callback_data="main_help")],
        home_button(),
    ])


def seller_dashboard_keyboard(record=None):
    """Single seller control centre used by /dashboard."""
    rows = []

    if record:
        active = bool(record.get("active"))
        rows.extend([
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
        rows.append([
            InlineKeyboardButton("➕ Create / Connect Child Bot", callback_data="seller_connect")
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
        f"🤖 Connected Child Bots: {bots}\n"
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
            "Tap “Create / Connect Child Bot”, create a bot from @BotFather, "
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
        "Open your child bot and send /admin for plan, payment, user, "
        "broadcast and channel controls."
    ), record


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
        [
            InlineKeyboardButton(
                "✅ Unsuspend Seller" if suspended else "🚫 Suspend Seller",
                callback_data=(
                    f"main_seller_unsuspend_{owner_id}"
                    if suspended else f"main_seller_suspend_{owner_id}"
                ),
            )
        ]
    ]

    if record:
        keyboard.append([
            InlineKeyboardButton(
                "⏹ Stop Child Bot",
                callback_data=f"main_seller_stopbot_{owner_id}",
            )
        ])

    keyboard.extend([
        [InlineKeyboardButton("⬅ Sellers", callback_data="main_owner_sellers")],
        [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
    ])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def main_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    user_id = query.from_user.id

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

    if action == "main_child_setup":
        record = await get_bot(user_id)
        await query.edit_message_text(
            "📖 Child Bot Setup Guide\n\n"
            "1. Open @BotFather\n"
            "2. Send /newbot\n"
            "3. Choose bot name and username\n"
            "4. Copy the token\n"
            "5. Return here and tap Create / Connect Child Bot\n"
            "6. Send the token\n"
            "7. Open the new child bot and send /admin\n\n"
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
            lines.append("No child bots connected.")
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
        CallbackQueryHandler(main_callbacks, pattern=r"^main_(?!home$).+"),
    ]
