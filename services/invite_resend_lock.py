import asyncio
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# Global per-user resend locks
_resend_locks = defaultdict(asyncio.Lock)


async def resend_invites_safely(user_id: int, invite_callback):
    """
    Prevent duplicate invite resends for the same user.
    `invite_callback` should be an async function that performs the resend.
    """
    lock = _resend_locks[int(user_id)]

    async with lock:
        logger.info("Resending invite links user_id=%s", user_id)
        return await invite_callback()
