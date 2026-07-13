from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from database.admins import is_admin
from database.seller_bots import get_bot


USER_HELP = """🆘 Main Bot Help

👤 User Commands
/start - Open the main menu
/help - Show this help guide
/seller - Open your seller dashboard

🤖 Connect a Child Bot
1. Open /seller
2. Tap Connect Bot
3. Send your BotFather token
4. The system verifies and starts the child bot

🏪 Seller Dashboard
• My Bot - Check bot username, status and runtime
• Pause Bot - Temporarily stop the child bot
• Resume Bot - Start it again
• Replace Token - Connect a different token
• Remove Bot - Remove the connected child bot

🔐 Safety
Only send a token created by you. Never share another person's token.
"""


SELLER_HELP = """🏪 Connected Seller Help

/seller - Open Seller Dashboard
/help - Show this help guide

After connecting your child bot:
• Open the child bot
• Send /admin to manage it
• Send /help in the child bot for complete seller-admin instructions

Main bot controls:
• My Bot
• Pause / Resume
• Replace Token
• Remove Bot
"""


ADMIN_HELP = """🛠 Main Bot Admin Help

/admin - Open the main admin panel
/help - Show this help guide
/seller - Open seller dashboard

Admin Panel Features
👥 User Management
Search by User ID or @username. Give, extend or remove subscriptions and ban/unban users.

➕ Add Channel/Group
Add a destination channel or group. The bot must be an administrator.

📋 Channel List
View and remove connected channels/groups.

💳 Payment Settings
Configure UPI information and payment QR.

📨 Pending Payments
Review screenshots and approve or reject payments.

📜 Payment History
See processed payments.

📢 Broadcast
Send announcements to users.

📊 Statistics
Check users, payments and revenue.

⚙️ Bot Settings
Manage available configuration.

👮 Admin Commands
Manage admin-only controls.
"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if await is_admin(user_id):
        text = USER_HELP + "\n━━━━━━━━━━━━━━━━━━━━\n" + ADMIN_HELP
        await update.effective_message.reply_text(text)
        return

    seller_bot = await get_bot(user_id)
    if seller_bot:
        text = USER_HELP + "\n━━━━━━━━━━━━━━━━━━━━\n" + SELLER_HELP
    else:
        text = USER_HELP

    await update.effective_message.reply_text(text)


def help_handler():
    return CommandHandler("help", help_command)
