from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE

scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def start_scheduler():
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
