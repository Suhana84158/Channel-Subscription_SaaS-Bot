from __future__ import annotations

import asyncio
import logging

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from database.broadcast import (
    claim_recipient,
    claim_run,
    finalize_if_done,
    finish_recipient,
    get_run,
    recoverable_runs,
    refresh_run_stats,
    renew_run_lease,
)

logger = logging.getLogger(__name__)
_tasks: dict[str, asyncio.Task] = {}


def _retry_seconds(exc: RetryAfter) -> int:
    value = exc.retry_after
    if hasattr(value, "total_seconds"):
        return max(1, int(value.total_seconds()))
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


async def _deliver(bot, run: dict, user_id: int) -> tuple[str, str | None, int | None]:
    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=run["source_chat_id"],
            message_id=run["source_message_id"],
        )
        return "sent", None, None
    except RetryAfter as exc:
        return "retry", str(exc), _retry_seconds(exc) + 1
    except Forbidden as exc:
        return "blocked", str(exc), None
    except BadRequest as exc:
        return "skipped", str(exc), None
    except NetworkError as exc:
        return "retry", str(exc), 5
    except TelegramError as exc:
        return "failed", str(exc), None
    except Exception as exc:
        logger.exception("Broadcast delivery failed user_id=%s", user_id)
        return "retry", str(exc), 10


async def process_broadcast(bot, broadcast_id: str) -> None:
    run = await claim_run(broadcast_id)
    if not run:
        return

    run_claim_token = str(run.get("run_claim_token", ""))
    try:
        handled = 0
        while True:
            current = await get_run(broadcast_id)
            if not current or current.get("status") == "cancelled":
                return
            if current.get("run_claim_token") != run_claim_token:
                # Another worker recovered the expired run lease.
                return

            recipient = await claim_recipient(broadcast_id)
            if not recipient:
                if await finalize_if_done(broadcast_id):
                    return
                await renew_run_lease(broadcast_id, run_claim_token)
                await asyncio.sleep(2)
                continue

            user_id = int(recipient["user_id"])
            claim_token = str(recipient.get("claim_token", ""))
            status, error, retry_after = await _deliver(bot, run, user_id)
            await finish_recipient(
                broadcast_id,
                user_id,
                claim_token,
                status,
                error,
                retry_after,
            )

            handled += 1
            if handled % 25 == 0:
                await refresh_run_stats(broadcast_id)
                renewed = await renew_run_lease(broadcast_id, run_claim_token)
                if not renewed:
                    return
            await asyncio.sleep(0.04)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Broadcast worker crashed broadcast_id=%s", broadcast_id)
    finally:
        try:
            await refresh_run_stats(broadcast_id)
        except Exception:
            logger.exception("Unable to refresh broadcast stats broadcast_id=%s", broadcast_id)


def start_broadcast_task(bot, broadcast_id: str) -> asyncio.Task:
    task = _tasks.get(broadcast_id)
    if task and not task.done():
        return task

    task = asyncio.create_task(
        process_broadcast(bot, broadcast_id),
        name=f"broadcast:{broadcast_id}",
    )
    _tasks[broadcast_id] = task

    def _cleanup(done_task: asyncio.Task) -> None:
        _tasks.pop(broadcast_id, None)
        if done_task.cancelled():
            return
        try:
            done_task.result()
        except Exception:
            logger.exception("Unhandled broadcast task error broadcast_id=%s", broadcast_id)

    task.add_done_callback(_cleanup)
    return task


async def resume_broadcasts(bot) -> int:
    runs = await recoverable_runs()
    for run in runs:
        start_broadcast_task(bot, run["broadcast_id"])
    return len(runs)
