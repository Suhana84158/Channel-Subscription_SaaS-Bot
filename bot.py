import asyncio
import logging

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from config import BOT_TOKEN
from database.admins import initialize_admins
from database.deleting_messages import initialize_deleting_message_indexes
from database.live_support import initialize_live_support_indexes
from database.mongo import close_database, connect_database
from database.payment_gateways import initialize_payment_gateway_indexes
from database.performance import initialize_performance_indexes
from database.platform_features import initialize_platform_feature_indexes
from database.seller_bots import initialize_seller_bot_indexes
from database.seller_data import initialize_seller_data_indexes
from database.seller_referrals import initialize_seller_referral_indexes
from database.seller_subscriptions import initialize_seller_subscription_indexes
from database.settings import initialize_default_settings
from handlers.admin import admin_handlers, receive_upi_qr
from handlers.broadcast import broadcast_handler
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
from handlers.seller_subscription_management import handlers as seller_subscription_management_handlers
from handlers.start import start_callback_handler, start_command
from handlers.statistics import statistics_handler
from handlers.subscription import subscription_callback
from handlers.support import support_callback, support_reply_handler
from handlers.upload_payment import payment_upload_handlers
from keep_alive import configure_runtime, keep_alive
from logging_config import setup_logging
from scheduler import add_cron_job, add_interval_job, shutdown_scheduler, start_scheduler
from scheduler_jobs.seller_subscriptions import run_seller_subscription_reminders
from services.bot_manager import bot_manager

logger = logging.getLogger(__name__)


async def _initialize_component(name: str, function, timeout: int = 30) -> None:
    logger.info("Initializing %s...", name)
    await asyncio.wait_for(function(), timeout=timeout)
    logger.info("Initialized %s.", name)


async def post_init(application: Application):
    logger.info("Connecting to MongoDB...")
    await connect_database()

    initializers = [
        ("admins", initialize_admins),
        ("default settings", initialize_default_settings),
        ("seller bot indexes", initialize_seller_bot_indexes),
        ("seller data indexes", initialize_seller_data_indexes),
        ("seller subscription indexes", initialize_seller_subscription_indexes),
        ("platform feature indexes", initialize_platform_feature_indexes),
        ("payment gateway indexes", initialize_payment_gateway_indexes),
        ("seller referral indexes", initialize_seller_referral_indexes),
        ("live support indexes", initialize_live_support_indexes),
        ("deleting message indexes", initialize_deleting_message_indexes),
        ("performance indexes", initialize_performance_indexes),
    ]
    for name, initializer in initializers:
        await _initialize_component(name, initializer)

    configure_runtime(asyncio.get_running_loop(), application.bot)
    start_scheduler()

    async def seller_subscription_reminder_job():
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

    restored = await bot_manager.restore_active_bots()
    logger.info("Seller bots restored: %s", restored)
    logger.info("Bot started successfully.")


async def post_shutdown(application: Application):
    logger.info("Bot shutdown started.")
    shutdown_scheduler()
    await bot_manager.shutdown_all()
    await close_database()
    logger.info("Bot shutdown completed.")


def build_application():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    return (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def register_handlers(application: Application):
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
    application.add_handler(MessageHandler(filters.PHOTO, receive_upi_qr), group=-1)

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
    logger.info("All handlers registered successfully.")


def main():
    setup_logging()
    logger.info("Starting Telegram Subscription Bot...")

    keep_alive()
    application = build_application()
    register_handlers(application)

    logger.info("Bot initialization completed. Starting polling.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
