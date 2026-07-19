import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

from pymongo import ReturnDocument

from database.mongo import get_database

logger = logging.getLogger(__name__)

T = TypeVar("T")
COLLECTION = "invite_resend_locks"
DEFAULT_LEASE_SECONDS = 15 * 60

# Fast in-process protection. MongoDB lease below protects multiple workers.
_resend_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lock_key(owner_id: int, bot_id: int) -> str:
    return f"{int(owner_id)}:{int(bot_id)}"


async def acquire_resend_lease(
    owner_id: int,
    bot_id: int,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> str | None:
    """Atomically acquire a cross-process resend lease."""
    now = _utcnow()
    token = uuid.uuid4().hex
    expires_at = now + timedelta(seconds=max(60, int(lease_seconds)))

    doc = await get_database()[COLLECTION].find_one_and_update(
        {
            "_id": _lock_key(owner_id, bot_id),
            "$or": [
                {"locked": {"$ne": True}},
                {"lease_expires_at": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "owner_id": int(owner_id),
                "bot_id": int(bot_id),
                "locked": True,
                "lease_token": token,
                "lease_expires_at": expires_at,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    if not doc or doc.get("lease_token") != token:
        return None
    return token


async def release_resend_lease(owner_id: int, bot_id: int, token: str) -> None:
    """Release only the lease owned by this caller."""
    now = _utcnow()
    await get_database()[COLLECTION].update_one(
        {
            "_id": _lock_key(owner_id, bot_id),
            "lease_token": str(token),
            "locked": True,
        },
        {
            "$set": {
                "locked": False,
                "released_at": now,
                "updated_at": now,
            },
            "$unset": {
                "lease_token": "",
                "lease_expires_at": "",
            },
        },
    )


async def resend_invites_safely(
    owner_id: int,
    bot_id: int,
    invite_callback: Callable[[], Awaitable[T]],
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[bool, T | None]:
    """
    Run one resend operation per seller/clone-bot across all workers.

    Returns ``(False, None)`` when another resend is already active.
    """
    key = _lock_key(owner_id, bot_id)
    local_lock = _resend_locks[key]

    if local_lock.locked():
        return False, None

    async with local_lock:
        token = await acquire_resend_lease(
            owner_id,
            bot_id,
            lease_seconds=lease_seconds,
        )
        if token is None:
            return False, None

        try:
            logger.info(
                "Invite resend started owner_id=%s bot_id=%s",
                owner_id,
                bot_id,
            )
            result = await invite_callback()
            return True, result
        finally:
            try:
                await release_resend_lease(owner_id, bot_id, token)
            except Exception:
                logger.exception(
                    "Failed releasing invite resend lease owner_id=%s bot_id=%s",
                    owner_id,
                    bot_id,
                )
