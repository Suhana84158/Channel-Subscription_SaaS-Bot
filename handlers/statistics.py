from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
)

from database.admins import is_admin
from database.users import total_users
from database.channels import total_channels
from database.payments import total_revenue
from database.subscriptions import subscriptions_collection


async def statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ You are not authorized."
        )
        return

    users = await total_users()
    channels = await total_channels()
    revenue = await total_revenue()

    active_subscriptions = (
        await subscriptions_collection().count_documents(
            {"active": True}
        )
    )

    text = (
        "📊 *Bot Statistics*\n\n"
        f"👥 Total Users: *{users}*\n"
        f"💎 Active Subscriptions: *{active_subscriptions}*\n"
        f"📢 Total Channels: *{channels}*\n"
        f"💰 Total Revenue: *₹{revenue}*"
    )

    await update.message.reply_text(
        text=text,
        parse_mode="Markdown",
    )


def statistics_handler():
    return CommandHandler(
        "stats",
        statistics,
    )
