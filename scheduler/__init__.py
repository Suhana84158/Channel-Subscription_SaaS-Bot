from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE, EXPIRY_CHECK_INTERVAL_MINUTES
from scheduler_jobs.expiry import check_expired_subscriptions


scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def start_scheduler():
    if scheduler.running:
        return

    scheduler.add_job(
        check_expired_subscriptions,
        trigger="interval",
        minutes=EXPIRY_CHECK_INTERVAL_MINUTES,
        id="subscription_expiry_check",
        replace_existing=True,
    )

    scheduler.start()


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
