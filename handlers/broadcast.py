import asyncio
from collections import defaultdict

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from database.admins import is_admin
from database.broadcast import (
    create_run,
    get_active_run_for_owner,
    get_latest_run_for_owner,
    get_run,
    refresh_run_stats,
    request_cancel,
)
from database.users import users_collection
from services.broadcast_service import start_broadcast_task

WAIT_BROADCAST = 1
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return ConversationHandler.END

    if _locks[user.id].locked():
        await message.reply_text("⏳ Previous broadcast is still being prepared.")
        return ConversationHandler.END

    active = await get_active_run_for_owner(user.id)
    if active:
        await message.reply_text(
            "ℹ️ A broadcast is already running.\n"
            f"ID: {active['broadcast_id'][:8]}\n"
            "Use /broadcast_status or /cancel_broadcast."
        )
        return ConversationHandler.END

    await message.reply_text("📢 Send the message to broadcast.\nCancel: /cancel")
    return WAIT_BROADCAST


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return ConversationHandler.END

    async with _locks[user.id]:
        active = await get_active_run_for_owner(user.id)
        if active:
            await message.reply_text(
                "ℹ️ A broadcast is already running.\n"
                f"ID: {active['broadcast_id'][:8]}"
            )
            return ConversationHandler.END

        cursor = users_collection().find(
            {"user_id": {"$type": "int"}},
            {"_id": 0, "user_id": 1},
        )
        user_ids = [row["user_id"] async for row in cursor]
        if not user_ids:
            await message.reply_text("ℹ️ No registered users found.")
            return ConversationHandler.END

        run = await create_run(user.id, message.chat_id, message.message_id, user_ids)
        start_broadcast_task(context.bot, run["broadcast_id"])
        await message.reply_text(
            "✅ Broadcast queued safely.\n"
            f"Users: {run['total']}\n"
            f"ID: {run['broadcast_id'][:8]}\n\n"
            "It will continue after a restart."
        )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message:
        await message.reply_text("❌ Broadcast preparation cancelled.")
    return ConversationHandler.END


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return

    run = await get_active_run_for_owner(user.id)
    if not run:
        await message.reply_text("ℹ️ No active broadcast found.")
        return

    changed = await request_cancel(run["broadcast_id"], user.id)
    await message.reply_text(
        "🛑 Broadcast cancelled." if changed else "ℹ️ Broadcast already finished."
    )


async def broadcast_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message or not await is_admin(user.id):
        return

    run = await get_latest_run_for_owner(user.id)
    if not run:
        await message.reply_text("ℹ️ No recent broadcast found.")
        return

    await refresh_run_stats(run["broadcast_id"])
    run = await get_run(run["broadcast_id"])
    if not run:
        await message.reply_text("ℹ️ Broadcast record not found.")
        return

    await message.reply_text(
        f"📢 Status: {run.get('status')}\n"
        f"ID: {run.get('broadcast_id', '')[:8]}\n"
        f"Progress: {run.get('processed', 0)}/{run.get('total', 0)}\n"
        f"✅ Sent: {run.get('sent', 0)}\n"
        f"🚫 Blocked: {run.get('blocked', 0)}\n"
        f"⏭ Skipped: {run.get('skipped', 0)}\n"
        f"❌ Failed: {run.get('failed', 0)}"
    )


def broadcast_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            WAIT_BROADCAST: [
                MessageHandler(filters.ALL & ~filters.COMMAND, send_broadcast)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=False,
    )


def broadcast_extra_handlers():
    return [
        CommandHandler("broadcast_status", broadcast_status),
        CommandHandler("cancel_broadcast", cancel_broadcast),
    ]
