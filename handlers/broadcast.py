import asyncio
from collections import defaultdict

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from database.admins import is_admin
from database.broadcast import create_run, get_latest_run_for_owner, get_run, request_cancel
from database.users import users_collection
from services.broadcast_service import start_broadcast_task

WAIT_BROADCAST = 1
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_active: dict[int, str] = {}


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return ConversationHandler.END
    if _locks[user.id].locked():
        await message.reply_text("⏳ Previous broadcast is still being prepared.")
        return ConversationHandler.END
    await message.reply_text("📢 Send the message to broadcast.\nCancel: /cancel")
    return WAIT_BROADCAST


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return ConversationHandler.END
    async with _locks[user.id]:
        cursor = users_collection().find({"user_id": {"$type": "int"}}, {"_id": 0, "user_id": 1})
        user_ids = [row["user_id"] async for row in cursor]
        if not user_ids:
            await message.reply_text("ℹ️ No registered users found.")
            return ConversationHandler.END
        run = await create_run(user.id, message.chat_id, message.message_id, user_ids)
        _active[user.id] = run["broadcast_id"]
        start_broadcast_task(context.bot, run["broadcast_id"])
        await message.reply_text(
            "✅ Broadcast queued safely.\n"
            f"Users: {run['total']}\n"
            f"ID: {run['broadcast_id'][:8]}\n\n"
            "It will continue after a restart."
        )
    return ConversationHandler.END


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return ConversationHandler.END
    broadcast_id = _active.get(user.id)
    if not broadcast_id:
        latest = await get_latest_run_for_owner(user.id)
        if latest and latest.get("status") in {"pending", "processing"}:
            broadcast_id = latest["broadcast_id"]
    if not broadcast_id:
        await message.reply_text("ℹ️ No active broadcast found.")
        return ConversationHandler.END
    changed = await request_cancel(broadcast_id, user.id)
    await message.reply_text("🛑 Broadcast cancelled." if changed else "ℹ️ Broadcast already finished.")
    return ConversationHandler.END


async def broadcast_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return
    broadcast_id = _active.get(user.id)
    if not broadcast_id:
        latest = await get_latest_run_for_owner(user.id)
        broadcast_id = latest.get("broadcast_id") if latest else None
    if not broadcast_id:
        await message.reply_text("ℹ️ No recent broadcast found.")
        return
    run = await get_run(broadcast_id)
    if not run:
        await message.reply_text("ℹ️ Broadcast record not found.")
        return
    await message.reply_text(
        f"📢 Status: {run.get('status')}\n"
        f"Progress: {run.get('processed', 0)}/{run.get('total', 0)}\n"
        f"✅ Sent: {run.get('sent', 0)}\n"
        f"🚫 Blocked: {run.get('blocked', 0)}\n"
        f"⏭ Skipped: {run.get('skipped', 0)}\n"
        f"❌ Failed: {run.get('failed', 0)}"
    )


def broadcast_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={WAIT_BROADCAST: [MessageHandler(filters.ALL & ~filters.COMMAND, send_broadcast)]},
        fallbacks=[CommandHandler("cancel", cancel_broadcast)],
        allow_reentry=False,
    )


def broadcast_extra_handlers():
    return [CommandHandler("broadcast_status", broadcast_status)]
