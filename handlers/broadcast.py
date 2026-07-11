from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ConversationHandler, ContextTypes, filters

from database.admins import is_admin
from database.users import users_collection

WAIT_BROADCAST = 1


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📢 Send broadcast message.\n\n"
        "Text, photo, video, document sab chalega."
    )
    return WAIT_BROADCAST


async def send_to_user(bot, chat_id, msg):
    caption = msg.caption or ""

    if msg.photo:
        await bot.send_photo(chat_id=chat_id, photo=msg.photo[-1].file_id, caption=caption)
    elif msg.video:
        await bot.send_video(chat_id=chat_id, video=msg.video.file_id, caption=caption)
    elif msg.document:
        await bot.send_document(chat_id=chat_id, document=msg.document.file_id, caption=caption)
    elif msg.animation:
        await bot.send_animation(chat_id=chat_id, animation=msg.animation.file_id, caption=caption)
    elif msg.text:
        await bot.send_message(chat_id=chat_id, text=msg.text)
    else:
        await bot.copy_message(chat_id=chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return ConversationHandler.END

    msg = update.message
    users = await users_collection().find().to_list(length=None)

    success = 0
    failed = 0

    progress = await msg.reply_text(f"📢 Broadcast started...\nTotal users: {len(users)}")

    for user in users:
        try:
            await send_to_user(context.bot, user["user_id"], msg)
            success += 1
        except Exception:
            failed += 1

    await progress.edit_text(
        f"✅ Broadcast completed.\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}"
    )

    return ConversationHandler.END


def broadcast_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            WAIT_BROADCAST: [
                MessageHandler(filters.ALL, send_broadcast),
            ]
        },
        fallbacks=[],
    )
