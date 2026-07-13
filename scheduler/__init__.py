from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE
from logging_config import get_logger
from scheduler.expiry_worker import check_expired_users

logger = get_logger(__name__)

scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def start_scheduler() -> None:
    """Start APScheduler and register the regular expiry check once."""
    if not scheduler.get_job("subscription_expiry_check"):
        scheduler.add_job(
            check_expired_users,
            trigger="interval",
            minutes=5,
            id="subscription_expiry_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def add_interval_job(
    func,
    job_id: str,
    minutes: int,
    replace_existing: bool = True,
) -> None:
    scheduler.add_job(
        func=func,
        trigger="interval",
        minutes=minutes,
        id=job_id,
        replace_existing=replace_existing,
        max_instances=1,
        coalesce=True,
    )


def add_cron_job(func, job_id: str, **kwargs) -> None:
    scheduler.add_job(
        func=func,
        trigger="cron",
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        **kwargs,
    )
