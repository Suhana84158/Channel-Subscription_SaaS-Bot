from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE
from logging_config import get_logger
from scheduler.expiry_worker import check_expired_users

logger = get_logger(__name__)

scheduler = AsyncIOScheduler(
    timezone=TIMEZONE,
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 120,
    },
)
_listener_added = False


def _job_listener(event) -> None:
    if event.exception:
        logger.error(
            "Scheduler job failed job_id=%s",
            event.job_id,
            exc_info=(type(event.exception), event.exception, event.exception.__traceback__),
        )
    elif event.code == EVENT_JOB_MISSED:
        logger.warning("Scheduler job was missed job_id=%s", event.job_id)


def start_scheduler() -> None:
    """Start APScheduler and register regular jobs once."""
    global _listener_added

    if not _listener_added:
        scheduler.add_listener(_job_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        _listener_added = True

    if not scheduler.get_job("subscription_expiry_check"):
        scheduler.add_job(
            check_expired_users,
            trigger="interval",
            minutes=1,
            id="subscription_expiry_check",
            replace_existing=True,
        )

    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def add_interval_job(func, job_id: str, minutes: int, replace_existing: bool = True) -> None:
    scheduler.add_job(
        func=func,
        trigger="interval",
        minutes=minutes,
        id=job_id,
        replace_existing=replace_existing,
    )


def add_cron_job(func, job_id: str, **kwargs) -> None:
    scheduler.add_job(
        func=func,
        trigger="cron",
        id=job_id,
        replace_existing=True,
        **kwargs,
    )
