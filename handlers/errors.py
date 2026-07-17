import asyncio
import traceback

from telegram import Update
from telegram.ext import ContextTypes

from database.logs import create_log
from logging_config import get_logger

logger = get_logger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    error_text = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    logger.error("Unhandled Telegram update error:\n%s", error_text)

    # Error reporting must never block Telegram update processing.
    try:
        await asyncio.wait_for(
            create_log(log_type="error", message=str(error)[:1000]),
            timeout=3,
        )
    except Exception:
        logger.warning("Could not persist error log to MongoDB.", exc_info=True)

    if isinstance(update, Update) and update.effective_message:
        try:
            await asyncio.wait_for(
                update.effective_message.reply_text(
                    "⚠️ Temporary problem occurred. Please try again in a few seconds."
                ),
                timeout=8,
            )
        except Exception:
            logger.debug("Could not send user-facing error message.", exc_info=True)
