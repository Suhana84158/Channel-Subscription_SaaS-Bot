from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database.admins import is_admin
from database.seller_bots import get_bot
from handlers.official_links import build_official_links_keyboard


async def build_help(user_id: int):
    if await is_admin(user_id):
        text=(
            "🆘 Owner Help & Commands\n\n"
            "/start - Open owner welcome menu\n"
            "/dashboard - Open owner dashboard\n"
            "/owner - Open owner dashboard\n"
            "/help - Show this guide\n"
            "/stats - Main bot statistics\n"
            "/broadcast - Broadcast from main bot\n\n"
            "Owner controls include users, sellers, Clone Bots, subscriptions, payments, broadcasts, statistics, monitoring, and official links."
        )
        links_kb=await build_official_links_keyboard()
        rows=list(links_kb.inline_keyboard) if links_kb else []
        rows += [[InlineKeyboardButton("👑 Owner Dashboard", callback_data="main_owner_dashboard")],[InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")]]
        return text, InlineKeyboardMarkup(rows)

    record = await get_bot(user_id)
    text=(
        "🆘 Seller Help & Commands\n\n"
        "/start - Open seller welcome menu\n"
        "/dashboard - Open seller dashboard\n"
        "/mybots - View and manage your Clone Bot\n"
        "/help - Show this guide\n\n"
        "Create a Clone Bot:\n"
        "1. Create a bot in @BotFather\n"
        "2. Tap Create / Connect Clone Bot\n"
        "3. Send your own BotFather token\n"
        "4. Open the Clone Bot and send /admin\n\n"
        "Clone Bot features include plans, channels/groups, payments, welcome editor, live support, broadcast, referrals, user management, and statistics."
    )
    links_kb=await build_official_links_keyboard()
    rows=list(links_kb.inline_keyboard) if links_kb else []
    rows += [[InlineKeyboardButton("🤖 Manage Clone Bot" if record else "➕ Create Clone Bot",callback_data="seller_my_bot" if record else "seller_connect")],[InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")],[InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")]]
    return text, InlineKeyboardMarkup(rows)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = await build_help(update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text, keyboard = await build_help(query.from_user.id)
    await query.edit_message_text(text, reply_markup=keyboard)


def help_handler():
    return CommandHandler("help", help_command)


def help_callback_handler():
    return CallbackQueryHandler(help_callback, pattern=r"^main_help$")
