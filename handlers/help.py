from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database.admins import is_admin
from database.seller_bots import get_bot
from handlers.official_links import build_official_links_keyboard


def _back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Help Center", callback_data="main_help")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_home")],
    ])


async def build_help(user_id: int):
    owner = await is_admin(user_id)
    record = None if owner else await get_bot(user_id)

    role = "Owner" if owner else "Seller"
    text = (
        f"📚 {role} Help Center\n\n"
        "Welcome! Choose a topic below to view a short step-by-step guide.\n\n"
        "🚀 New here? Start with Quick Start.\n"
        "🛠 Need a command? Open Commands.\n"
        "🧪 Something not working? Open Troubleshooting."
    )

    rows = [
        [InlineKeyboardButton("🚀 Quick Start", callback_data="main_help_quick"),
         InlineKeyboardButton("🛠 Commands", callback_data="main_help_commands")],
        [InlineKeyboardButton("🤖 Clone Bot", callback_data="main_help_clone"),
         InlineKeyboardButton("👥 Subscribers", callback_data="main_help_users")],
        [InlineKeyboardButton("💳 Payments", callback_data="main_help_payments"),
         InlineKeyboardButton("📂 Channels & Groups", callback_data="main_help_channels")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="main_help_broadcast"),
         InlineKeyboardButton("🎫 Live Support", callback_data="main_help_support")],
        [InlineKeyboardButton("⚙ Settings", callback_data="main_help_settings"),
         InlineKeyboardButton("❓ FAQ", callback_data="main_help_faq")],
        [InlineKeyboardButton("🧪 Troubleshooting", callback_data="main_help_trouble"),
         InlineKeyboardButton("🆕 What's New", callback_data="main_help_new")],
        [InlineKeyboardButton("📜 Changelog", callback_data="main_help_changelog"),
         InlineKeyboardButton("📞 Contact Support", callback_data="main_help_contact")],
    ]

    links_kb = await build_official_links_keyboard()
    if links_kb:
        rows.extend(list(links_kb.inline_keyboard))

    if owner:
        rows.append([InlineKeyboardButton("👑 Owner Dashboard", callback_data="main_owner_dashboard")])
    else:
        rows.append([
            InlineKeyboardButton(
                "🤖 Manage Clone Bot" if record else "➕ Create Clone Bot",
                callback_data="seller_my_bot" if record else "seller_connect",
            )
        ])
        rows.append([InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")])

    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_home")])
    return text, InlineKeyboardMarkup(rows)


async def _topic_text(user_id: int, topic: str) -> str:
    owner = await is_admin(user_id)

    topics = {
        "quick": (
            "🚀 Quick Start Guide\n\n"
            "1️⃣ Create a bot using @BotFather.\n"
            "2️⃣ Open Seller Dashboard and connect the bot token.\n"
            "3️⃣ Open your Clone Bot and use /admin.\n"
            "4️⃣ Add subscription plans.\n"
            "5️⃣ Connect your channel or group.\n"
            "6️⃣ Configure UPI/QR or an automatic gateway.\n"
            "7️⃣ Test payment, approval and invite delivery before launch."
        ),
        "commands": (
            "🛠 Commands\n\n"
            "👤 Common Commands\n"
            "/start — Open the correct home screen\n"
            "/help — Open this Help Center\n"
            "/cancel — Cancel the current input when supported\n\n"
            "🏪 Seller Commands\n"
            "/dashboard — Open Seller Dashboard\n"
            "/mybots — View or manage your Clone Bot\n\n"
            "🤖 Clone Bot Admin Commands\n"
            "/admin — Open Clone Bot Admin Panel\n"
            "/connectgroup — Connect a private group\n"
            "/connectsupport — Connect the Live Support group\n"
            "/version — Show deployed runtime version\n\n"
            + ("👑 Owner Commands\n/owner — Owner Dashboard\n/stats — Main bot statistics\n/broadcast — Main bot broadcast" if owner else "Only commands available for your account are shown here.")
        ),
        "clone": (
            "🤖 Clone Bot Guide\n\n"
            "• Create a bot with @BotFather.\n"
            "• Send only your own bot token during connection.\n"
            "• Open the connected bot and send /admin.\n"
            "• Configure plans, payments, channels/groups and welcome message.\n"
            "• Test every user flow before sharing the bot."
        ),
        "users": (
            "👥 Subscribers Guide\n\n"
            "Use User Management to search by Telegram ID or username.\n\n"
            "You can give, extend or remove subscriptions, ban/unban users, review expiry details and resend access links to active subscribers."
        ),
        "payments": (
            "💳 Payment Guide\n\n"
            "Manual payment: set UPI ID, UPI name and QR image. Users upload payment screenshots for approval.\n\n"
            "Automatic payment: configure supported gateway credentials, enable the gateway and complete a small test payment before launch.\n\n"
            "Use Pending Payments for approval and Payment History for processed records."
        ),
        "channels": (
            "📂 Channels & Groups Guide\n\n"
            "Add the Clone Bot as an administrator with Invite Users and Ban Users permissions.\n\n"
            "For a private group, send /connectgroup inside that group. You may also use:\n"
            "-1001234567890 | Group Name\n\n"
            "After replacing a connected chat, use Resend Invite Links for active subscribers."
        ),
        "broadcast": (
            "📢 Broadcast Guide\n\n"
            "Send text, photos, videos, documents and other supported Telegram messages.\n\n"
            "Use Scheduled Broadcast to send later. Review delivery results and use Retry Failed when needed. Always test with a small audience first."
        ),
        "support": (
            "🎫 Live Support Guide\n\n"
            "1️⃣ Enable Live Support and Topic Mode.\n"
            "2️⃣ Create a Telegram forum group.\n"
            "3️⃣ Add the Clone Bot as admin.\n"
            "4️⃣ Send /connectsupport inside the group.\n"
            "5️⃣ Reply inside each user's topic.\n\n"
            "Reply Templates and Template Auto Remove are available from the Live Support settings."
        ),
        "settings": (
            "⚙ Settings Guide\n\n"
            "Configure bot name, welcome text/media/buttons, timezone, reminders, support, referrals, content protection and message-deletion options.\n\n"
            "Use Preview after editing the welcome message and test all custom buttons."
        ),
        "faq": (
            "❓ Frequently Asked Questions\n\n"
            "Why is my group not connecting?\n"
            "→ Check admin permissions and send /connectgroup inside the group.\n\n"
            "Why was no invite link sent?\n"
            "→ The bot needs invite permission and the connected chat must still exist.\n\n"
            "Why is payment waiting?\n"
            "→ Manual screenshots require seller approval.\n\n"
            "Why is a Clone Bot offline?\n"
            "→ Check token validity and redeploy/runtime logs."
        ),
        "trouble": (
            "🧪 Troubleshooting\n\n"
            "Bot not replying:\n"
            "• Check Render/runtime status and recent logs.\n"
            "• Verify the bot token has not changed.\n\n"
            "Group or invite problem:\n"
            "• Recheck administrator permissions.\n"
            "• Remove and re-add the bot if permissions are stale.\n\n"
            "Payment problem:\n"
            "• Confirm UPI/QR or gateway credentials.\n"
            "• Test with a small payment.\n\n"
            "Live Support problem:\n"
            "• Confirm forum topics are enabled and reconnect the support group."
        ),
        "new": (
            "🆕 What's New\n\n"
            "• Improved Help Center and organized commands\n"
            "• Live Support Reply Templates\n"
            "• Template Auto Remove controls\n"
            "• Stability and recovery improvements\n\n"
            "Available features can depend on the deployed project version."
        ),
        "changelog": (
            "📜 Changelog\n\n"
            "Latest help update:\n"
            "• Added topic-based Help Center\n"
            "• Added Quick Start, FAQ and Troubleshooting\n"
            "• Organized owner, seller, admin and user commands\n"
            "• Added clearer setup instructions"
        ),
        "contact": (
            "📞 Contact Support\n\n"
            "Use the official support button below the Help Center when available.\n\n"
            "When reporting a problem, include:\n"
            "• What you tapped or sent\n"
            "• What you expected\n"
            "• What happened instead\n"
            "• A screenshot and recent error log, without private credentials"
        ),
    }
    return topics.get(topic, "📚 Help topic not found.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = await build_help(update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "main_help":
        text, keyboard = await build_help(query.from_user.id)
    else:
        topic = query.data.removeprefix("main_help_")
        text = await _topic_text(query.from_user.id, topic)
        keyboard = _back_home()

    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception:
        await query.message.reply_text(text, reply_markup=keyboard)


def help_handler():
    return CommandHandler("help", help_command)


def help_callback_handler():
    return CallbackQueryHandler(help_callback, pattern=r"^main_help(?:_[a-z]+)?$")
