import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from config import ADMIN_IDS, BOT_TOKEN
from database.admins import initialize_admins
from database.deleting_messages import initialize_deleting_message_indexes
from database.broadcast import initialize_broadcast_indexes
from database.live_support import initialize_live_support_indexes
from database.mongo import close_database, connect_database, ping_database
from database.payment_gateways import initialize_payment_gateway_indexes
from database.performance import initialize_performance_indexes
from database.platform_features import initialize_platform_feature_indexes
from database.seller_bots import initialize_seller_bot_indexes
from database.seller_data import initialize_seller_data_indexes
from database.seller_referrals import initialize_seller_referral_indexes
from database.seller_subscriptions import initialize_seller_subscription_indexes
from database.settings import initialize_default_settings
from handlers.admin import admin_handlers, receive_upi_qr
from handlers.broadcast import broadcast_extra_handlers, broadcast_handler
from handlers.errors import error_handler
from handlers.help import help_callback_handler, help_handler
from handlers.main_dashboard import main_dashboard_handlers
from handlers.official_links import handlers as official_links_handlers
from handlers.payment import payment_handler
from handlers.payment_approval import payment_approval_handlers
from handlers.payment_gateways import handlers as payment_gateway_handlers
from handlers.plans import plans_handler
from handlers.platform_features import handlers as platform_feature_handlers
from handlers.profile import profile_callback
from handlers.referral import referral_callback
from handlers.seller import seller_handlers
from handlers.seller_subscription_management import (
    handlers as seller_subscription_management_handlers,
)
from handlers.start import start_callback_handler, start_command
from handlers.statistics import statistics_handler
from handlers.subscription import subscription_callback
from handlers.support import support_callback, support_reply_handler
from handlers.upload_payment import payment_upload_handlers
from keep_alive import configure_runtime, keep_alive
from logging_config import setup_logging
from scheduler import (
    add_cron_job,
    add_interval_job,
    shutdown_scheduler,
    start_scheduler,
)
from scheduler_jobs.seller_subscriptions import run_seller_subscription_reminders
from services.bot_manager import bot_manager
from services.broadcast_service import resume_broadcasts

logger = logging.getLogger(__name__)

Initializer = Callable[[], Awaitable[Any]]


def _validate_startup_config() -> None:
    """Fail early when critical startup configuration is invalid."""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    # Telegram bot tokens normally contain a numeric bot ID and a secret part.
    if ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN format is invalid.")

    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty. Owner/admin-only controls may not be accessible."
        )


async def _initialize_component(
    name: str,
    function: Initializer,
    *,
    timeout: float = 30,
    attempts: int = 2,
    critical: bool = False,
) -> bool:
    """Initialize one startup component with bounded retry and clear logging."""
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            logger.info(
                "Initializing component=%s attempt=%s/%s",
                name,
                attempt,
                attempts,
            )
            await asyncio.wait_for(function(), timeout=timeout)
            logger.info("Initialized component=%s", name)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            logger.exception(
                "Initialization failed component=%s attempt=%s/%s critical=%s",
                name,
                attempt,
                attempts,
                critical,
            )

            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 5))

    if critical:
        raise RuntimeError(
            f"Critical startup component failed: {name}"
        ) from last_error

    logger.error(
        "Continuing without non-critical startup component=%s",
        name,
    )
    return False


async def post_init(application: Application) -> None:
    """Initialize database, indexes, scheduler, web runtime and clone bots."""
    logger.info("Application post-init started.")

    await _initialize_component(
        "MongoDB connection",
        connect_database,
        timeout=60,
        attempts=5,
        critical=True,
    )

    if not await ping_database(timeout=8):
        raise RuntimeError("MongoDB connected but health ping failed.")

    # These are required for correct owner access and basic application settings.
    critical_initializers: list[tuple[str, Initializer]] = [
        ("admins", initialize_admins),
        ("default settings", initialize_default_settings),
    ]

    # Index creation improves safety/performance, but one failed optional index
    # must not make the complete Telegram bot unavailable.
    optional_initializers: list[tuple[str, Initializer]] = [
        ("seller bot indexes", initialize_seller_bot_indexes),
        ("seller data indexes", initialize_seller_data_indexes),
        ("seller subscription indexes", initialize_seller_subscription_indexes),
        ("platform feature indexes", initialize_platform_feature_indexes),
        ("payment gateway indexes", initialize_payment_gateway_indexes),
        ("seller referral indexes", initialize_seller_referral_indexes),
        ("live support indexes", initialize_live_support_indexes),
        ("deleting message indexes", initialize_deleting_message_indexes),
        ("performance indexes", initialize_performance_indexes),
        ("broadcast queue indexes", initialize_broadcast_indexes),
    ]

    for name, initializer in critical_initializers:
        await _initialize_component(
            name,
            initializer,
            timeout=30,
            attempts=3,
            critical=True,
        )

    optional_success = 0
    for name, initializer in optional_initializers:
        if await _initialize_component(
            name,
            initializer,
            timeout=30,
            attempts=2,
            critical=False,
        ):
            optional_success += 1

    configure_runtime(asyncio.get_running_loop(), application.bot)

    try:
        start_scheduler()

        async def seller_subscription_reminder_job() -> None:
            await run_seller_subscription_reminders(application.bot)

        add_cron_job(
            seller_subscription_reminder_job,
            "seller_subscription_reminders",
            hour=9,
            minute=0,
        )
        add_interval_job(
            bot_manager.recover_dead_bots,
            "clone_bot_runtime_watchdog",
            minutes=2,
        )
    except Exception:
        logger.exception("Scheduler startup failed.")
        raise RuntimeError("Unable to start scheduler.") from None

    resumed_broadcasts = await resume_broadcasts(application.bot)
    logger.info("Resumed broadcast queues=%s", resumed_broadcasts)

    restored = 0
    try:
        restored = await asyncio.wait_for(
            bot_manager.restore_active_bots(),
            timeout=180,
        )
    except asyncio.TimeoutError:
        logger.error(
            "Clone bot restore timed out after 180 seconds. "
            "The runtime watchdog will retry dead bots."
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Clone bot restore failed. Main bot will continue and watchdog "
            "will retry recovery."
        )

    logger.info(
        "Application post-init completed optional_indexes=%s/%s "
        "clone_bots_restored=%s",
        optional_success,
        len(optional_initializers),
        restored,
    )


async def post_shutdown(application: Application) -> None:
    """Best-effort shutdown: every component gets a chance to close."""
    logger.info("Application shutdown started.")

    try:
        shutdown_scheduler()
    except Exception:
        logger.exception("Scheduler shutdown failed.")

    try:
        await asyncio.wait_for(bot_manager.shutdown_all(), timeout=90)
    except asyncio.TimeoutError:
        logger.error("Clone bot shutdown timed out after 90 seconds.")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Clone bot shutdown failed.")

    try:
        await close_database()
    except Exception:
        logger.exception("MongoDB shutdown failed.")

    logger.info("Application shutdown completed.")


def build_application() -> Application:
    _validate_startup_config()

    return (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def register_handlers(application: Application) -> None:
    """Register handlers in a deterministic priority order."""
    for handler in seller_handlers():
        application.add_handler(handler, group=-10)

    application.add_handler(start_command())
    application.add_handler(help_handler())
    application.add_handler(help_callback_handler())
    application.add_handler(start_callback_handler())

    for handler in main_dashboard_handlers():
        application.add_handler(handler, group=-20)
    for handler in seller_subscription_management_handlers():
        application.add_handler(handler, group=-5)
    for handler in platform_feature_handlers():
        application.add_handler(handler, group=-5)
    for handler in official_links_handlers():
        application.add_handler(handler, group=-25)
    for handler in payment_gateway_handlers():
        application.add_handler(handler, group=-6)

    application.add_handler(plans_handler())
    application.add_handler(profile_callback())
    application.add_handler(payment_handler())
    application.add_handler(subscription_callback())
    application.add_handler(referral_callback())

    application.add_handler(broadcast_handler())
    application.add_handlers(broadcast_extra_handlers())
    application.add_handler(
        MessageHandler(filters.PHOTO, receive_upi_qr),
        group=-1,
    )

    for handler in payment_upload_handlers():
        application.add_handler(handler)

    application.add_handler(statistics_handler())
    application.add_handler(support_callback())
    application.add_handler(support_reply_handler())

    for handler in payment_approval_handlers():
        application.add_handler(handler)
    for handler in admin_handlers():
        application.add_handler(handler)

    application.add_error_handler(error_handler)

    handler_count = sum(
        len(group_handlers)
        for group_handlers in application.handlers.values()
    )
    logger.info(
        "Handlers registered successfully groups=%s handlers=%s",
        len(application.handlers),
        handler_count,
    )


def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Subscription SaaS Bot.")

    try:
        _validate_startup_config()
        keep_alive()

        application = build_application()
        register_handlers(application)

        logger.info("Initialization completed. Starting Telegram polling.")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            bootstrap_retries=-1,
        )
    except KeyboardInterrupt:
        logger.info("Shutdown requested by keyboard interrupt.")
    except Exception:
        logger.critical("Application stopped because of a fatal error.", exc_info=True)
        raise
    finally:
        logger.info("Main process finished.")


if __name__ == "__main__":
    main()
