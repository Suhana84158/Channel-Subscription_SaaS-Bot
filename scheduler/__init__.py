
import asyncio

from scheduler.expiry_worker import check_expired_users
from logging_config import get_logger

logger = get_logger(__name__)


async def expiry_job():
    while True:
        try:
            await check_expired_users()
        except Exception as e:
            logger.exception(e)

        await asyncio.sleep(300)


def start_scheduler():
    asyncio.create_task(expiry_job())
    logger.info("Scheduler started")
