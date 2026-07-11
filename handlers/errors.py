import traceback

from telegram import Update
from telegram.ext import ContextTypes

from logging_config import get_logger
from database.logs import create_log

logger = get_logger(__name__)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error_text = "".join(
        traceback.format_exception(
            None,
            context.error,
            context.error.__traceback__,
        )
    )

    logger.error(error_text)

    await create_log(
        log_type="error",
        message=str(context.error),
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ An unexpected error occurred.\nPlease try again later."
            )
        except Exception:
            pass
