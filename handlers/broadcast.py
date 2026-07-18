import asyncio
import logging
from collections import defaultdict

from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database.admins import is_admin
from database.users import users_collection
from database.platform_features import (
    broadcast_cancel_requested,
    create_broadcast_run,
    finalize_broadcast_run,
    request_broadcast_cancel,
    update_broadcast_progress,
)

logger = logging.getLogger(__name__)

WAIT_BROADCAST = 1
_PROGRESS_EVERY = 50
_SEND_DELAY_SECONDS = 0.04
_MAX_NETWORK_RETRIES = 3

# Prevent the same admin from starting overlapping broadcasts in one process.
_broadcast_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_active_broadcasts: dict[int, str] = {}


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None or not await is_admin(user.id):
        if message is not None:
            await message.reply_text("❌ You are not authorized.")
        return ConversationHandler.END

    lock = _broadcast_locks[user.id]
    if lock.locked():
        await message.reply_text(
            "⏳ Your previous broadcast is still running. Please wait for it to finish."
        )
        return ConversationHandler.END

    await message.reply_text(
        "📢 Send broadcast message.\n\n"
        "Text, photo, video, document sab chalega.\n"
        "Cancel karne ke liye /cancel bhejo."
    )
    return WAIT_BROADCAST


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None or not await is_admin(user.id):
        return ConversationHandler.END

    broadcast_id = _active_broadcasts.get(user.id)
    if not broadcast_id:
        await message.reply_text("ℹ️ No running broadcast found.")
        return ConversationHandler.END

    changed = await request_broadcast_cancel(broadcast_id, user.id)
    await message.reply_text(
        "🛑 Broadcast cancellation requested."
        if changed
        else "ℹ️ Broadcast is already finishing or cancelled."
    )
    return ConversationHandler.END


async def send_to_user(bot, chat_id: int, msg) -> None:
    """Copy the source message while preserving all Telegram-supported media."""
    await bot.copy_message(
        chat_id=chat_id,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
    )


def _retry_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    if hasattr(value, "total_seconds"):
        return max(float(value.total_seconds()), 1.0)
    try:
        return max(float(value), 1.0)
    except (TypeError, ValueError):
        return 1.0


async def _send_with_retry(bot, chat_id: int, msg) -> str:
    """Return sent, blocked, skipped, or failed without hiding the cause."""
    network_attempt = 0

    while True:
        try:
            await send_to_user(bot, chat_id, msg)
            return "sent"

        except RetryAfter as exc:
            wait_seconds = _retry_seconds(exc)
            logger.warning(
                "Broadcast rate-limited chat_id=%s retry_after=%.2fs",
                chat_id,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds + 0.5)

        except Forbidden:
            logger.info("Broadcast blocked by user chat_id=%s", chat_id)
            return "blocked"

        except BadRequest as exc:
            # Typical permanent cases: chat not found, user deactivated, invalid chat.
            logger.info(
                "Broadcast skipped chat_id=%s reason=%s",
                chat_id,
                str(exc),
            )
            return "skipped"

        except NetworkError as exc:
            network_attempt += 1
            if network_attempt > _MAX_NETWORK_RETRIES:
                logger.warning(
                    "Broadcast network retries exhausted chat_id=%s error=%s",
                    chat_id,
                    exc,
                )
                return "failed"

            delay = min(2**network_attempt, 8)
            logger.warning(
                "Broadcast network retry chat_id=%s attempt=%s delay=%ss",
                chat_id,
                network_attempt,
                delay,
            )
            await asyncio.sleep(delay)

        except TelegramError as exc:
            logger.warning(
                "Broadcast Telegram error chat_id=%s error=%s",
                chat_id,
                exc,
            )
            return "failed"

        except Exception:
            logger.exception("Unexpected broadcast failure chat_id=%s", chat_id)
            return "failed"


async def _safe_progress_update(progress, text: str) -> None:
    try:
        await progress.edit_text(text)
    except BadRequest as exc:
        # Telegram raises this when the text did not change; it is harmless.
        if "message is not modified" not in str(exc).lower():
            logger.warning("Unable to update broadcast progress: %s", exc)
    except TelegramError as exc:
        logger.warning("Unable to update broadcast progress: %s", exc)


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message

    if user is None or msg is None or not await is_admin(user.id):
        return ConversationHandler.END

    lock = _broadcast_locks[user.id]
    if lock.locked():
        await msg.reply_text(
            "⏳ Your previous broadcast is still running. Please wait for it to finish."
        )
        return ConversationHandler.END

    async with lock:
        collection = users_collection()
        total = await collection.count_documents({"user_id": {"$exists": True}})

        if total == 0:
            await msg.reply_text("ℹ️ No registered users found.")
            return ConversationHandler.END

        stats = {
            "sent": 0,
            "failed": 0,
            "blocked": 0,
            "skipped": 0,
            "processed": 0,
        }

        run = await create_broadcast_run(
            owner_id=user.id,
            total=total,
            scope="main",
        )
        broadcast_id = run["broadcast_id"]
        _active_broadcasts[user.id] = broadcast_id

        progress = await msg.reply_text(
            f"📢 Broadcast started...\nTotal users: {total}"
        )

        logger.info(
            "Broadcast started admin_id=%s total_users=%s source_message_id=%s",
            user.id,
            total,
            msg.message_id,
        )

        cursor = collection.find(
            {"user_id": {"$exists": True}},
            {"_id": 0, "user_id": 1},
        )

        try:
            async for record in cursor:
                if await broadcast_cancel_requested(broadcast_id):
                    logger.info(
                        "Broadcast cancellation observed admin_id=%s "
                        "broadcast_id=%s processed=%s",
                        user.id,
                        broadcast_id,
                        stats["processed"],
                    )
                    break

                chat_id = record.get("user_id")
                if not isinstance(chat_id, int):
                    stats["skipped"] += 1
                    stats["processed"] += 1
                    continue

                result = await _send_with_retry(context.bot, chat_id, msg)
                stats[result] += 1
                stats["processed"] += 1

                if (
                    stats["processed"] % _PROGRESS_EVERY == 0
                    or stats["processed"] == total
                ):
                    await update_broadcast_progress(
                        broadcast_id,
                        stats,
                    )
                    await _safe_progress_update(
                        progress,
                        "📢 Broadcast running...\n\n"
                        f"Progress: {stats['processed']}/{total}\n"
                        f"✅ Sent: {stats['sent']}\n"
                        f"🚫 Blocked: {stats['blocked']}\n"
                        f"⏭ Skipped: {stats['skipped']}\n"
                        f"❌ Failed: {stats['failed']}",
                    )

                await asyncio.sleep(_SEND_DELAY_SECONDS)

        except Exception:
            logger.exception(
                "Broadcast loop crashed admin_id=%s processed=%s total=%s",
                user.id,
                stats["processed"],
                total,
            )
            stats["failed"] += max(total - stats["processed"], 0)

        cancelled = await broadcast_cancel_requested(broadcast_id)
        final_status = "cancelled" if cancelled else "completed"

        await finalize_broadcast_run(
            broadcast_id,
            final_status,
            stats,
        )

        heading = (
            "🛑 Broadcast cancelled."
            if cancelled
            else "✅ Broadcast completed."
        )
        await _safe_progress_update(
            progress,
            f"{heading}\n\n"
            f"👥 Total Users: {total}\n"
            f"📌 Processed: {stats['processed']}\n"
            f"✅ Sent: {stats['sent']}\n"
            f"🚫 Blocked: {stats['blocked']}\n"
            f"⏭ Skipped: {stats['skipped']}\n"
            f"❌ Failed: {stats['failed']}",
        )

        _active_broadcasts.pop(user.id, None)

        logger.info(
            "Broadcast finished admin_id=%s broadcast_id=%s status=%s "
            "total=%s processed=%s sent=%s blocked=%s skipped=%s failed=%s",
            user.id,
            broadcast_id,
            final_status,
            total,
            stats["processed"],
            stats["sent"],
            stats["blocked"],
            stats["skipped"],
            stats["failed"],
        )

    return ConversationHandler.END


def broadcast_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            WAIT_BROADCAST: [
                MessageHandler(filters.ALL & ~filters.COMMAND, send_broadcast),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_broadcast)],
        allow_reentry=False,
    )
