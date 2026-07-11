from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import TIMEZONE

scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def start_scheduler():
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def add_interval_job(
    func,
    job_id: str,
    minutes: int,
    replace_existing: bool = True,
):
    scheduler.add_job(
        func=func,
        trigger="interval",
        minutes=minutes,
        id=job_id,
        replace_existing=replace_existing,
    )


def add_cron_job(
    func,
    job_id: str,
    **kwargs,
):
    scheduler.add_job(
        func=func,
        trigger="cron",
        id=job_id,
        replace_existing=True,
        **kwargs,
    )
