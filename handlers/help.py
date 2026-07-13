from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database.admins import is_admin
from database.seller_bots import get_bot


async def build_help(user_id: int):
    if await is_admin(user_id):
        return (
            "🆘 Owner Help & Commands\n\n"
            "/start - Open owner welcome menu\n"
            "/dashboard - Open owner dashboard\n"
            "/owner - Open owner dashboard\n"
            "/help - Show this guide\n"
            "/stats - Main bot statistics\n"
            "/broadcast - Broadcast from main bot\n"
            "/addadmin USER_ID - Add main admin\n"
            "/removeadmin USER_ID - Remove main admin\n"
            "/addchannel - Add main channel/group\n"
            "/removechannel CHAT_ID - Remove main channel/group\n\n"
            "Owner controls:\n"
            "• Main users and subscriptions\n"
            "• Sellers and their child bots\n"
            "• Payments, broadcasts and statistics\n"
            "• Suspend sellers and stop their child bots",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Owner Dashboard", callback_data="main_owner_dashboard")],
                [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
            ]),
        )

    record = await get_bot(user_id)
    return (
        "🆘 Seller Help & Commands\n\n"
        "/start - Open seller welcome menu\n"
        "/dashboard - Open seller dashboard\n"
        "/seller - Open seller bot controls\n"
        "/mybots - View and manage your child bot\n"
        "/help - Show this guide\n\n"
        "Create a child bot:\n"
        "1. Create a bot in @BotFather\n"
        "2. Tap Create / Connect Child Bot\n"
        "3. Send your own BotFather token\n"
        "4. Open the child bot and send /admin\n\n"
        "Inside the child bot:\n"
        "/start - User welcome page\n"
        "/admin - Seller admin panel\n"
        "/help - Child bot user/admin guide\n"
        "/version - Deployed child-bot version\n\n"
        "Child admin features include plans, channels/groups, payments, "
        "welcome editor, broadcast, referrals, user management and statistics.",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🤖 Manage Child Bot" if record else "➕ Create Child Bot",
                    callback_data="seller_my_bot" if record else "seller_connect",
                )
            ],
            [InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")],
            [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
        ]),
    )


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
