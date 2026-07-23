import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE
from scheduler.expiry_worker import check_expired_users
from scheduler.payment_recovery import recover_payments_job
from scheduler.gateway_recovery import (
    recover_gateway_transactions_job,
    recover_failed_invite_deliveries_job,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(
    timezone=TIMEZONE,
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 120,
    },
)

_listener_added = False
_watchdog_task: asyncio.Task | None = None
_shutdown_requested = False
_started_at: datetime | None = None
_last_restart_at: datetime | None = None
_restart_count = 0

# Job definitions are retained so a recovered scheduler can restore every job.
_registered_jobs: dict[str, dict[str, Any]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _job_listener(event) -> None:
    if event.exception:
        logger.error(
            "Scheduler job failed job_id=%s",
            event.job_id,
            exc_info=(
                type(event.exception),
                event.exception,
                event.exception.__traceback__,
            ),
        )
    elif event.code == EVENT_JOB_MISSED:
        logger.warning("Scheduler job was missed job_id=%s", event.job_id)


def _ensure_listener() -> None:
    global _listener_added

    if _listener_added:
        return

    scheduler.add_listener(
        _job_listener,
        EVENT_JOB_ERROR | EVENT_JOB_MISSED,
    )
    _listener_added = True


def _remember_job(
    *,
    func: Callable,
    trigger: str,
    job_id: str,
    replace_existing: bool = True,
    **kwargs,
) -> None:
    _registered_jobs[job_id] = {
        "func": func,
        "trigger": trigger,
        "id": job_id,
        "replace_existing": replace_existing,
        **kwargs,
    }


def _register_job(definition: dict[str, Any]) -> None:
    scheduler.add_job(**definition)


def _restore_registered_jobs() -> None:
    for job_id, definition in list(_registered_jobs.items()):
        try:
            _register_job(definition)
        except Exception:
            logger.exception(
                "Unable to restore scheduler job job_id=%s",
                job_id,
            )


def _register_core_jobs() -> None:
    _remember_job(
        func=check_expired_users,
        trigger="interval",
        minutes=1,
        job_id="subscription_expiry_check",
        replace_existing=True,
    )
    _remember_job(
        func=recover_payments_job,
        trigger="interval",
        minutes=5,
        job_id="payment_orphan_recovery",
        replace_existing=True,
    )
    _remember_job(
        func=recover_gateway_transactions_job,
        trigger="interval",
        minutes=2,
        job_id="gateway_transaction_recovery",
        replace_existing=True,
    )
    _remember_job(
        func=recover_failed_invite_deliveries_job,
        trigger="interval",
        minutes=2,
        job_id="gateway_invite_delivery_recovery",
        replace_existing=True,
    )
    _restore_registered_jobs()


async def _scheduler_watchdog() -> None:
    global _last_restart_at, _restart_count

    logger.info("Scheduler watchdog started")

    while not _shutdown_requested:
        try:
            await asyncio.sleep(60)

            if _shutdown_requested:
                break

            if scheduler.running:
                continue

            logger.warning(
                "[RECOVERY] Scheduler stopped unexpectedly; restarting"
            )

            try:
                scheduler.start()
                _restore_registered_jobs()
                _restart_count += 1
                _last_restart_at = _utcnow()

                logger.info(
                    "[RECOVERY] Scheduler restarted successfully "
                    "restart_count=%s jobs=%s",
                    _restart_count,
                    len(scheduler.get_jobs()),
                )
            except Exception:
                logger.exception(
                    "[RECOVERY] Scheduler restart failed"
                )

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Scheduler watchdog check failed")

    logger.info("Scheduler watchdog stopped")


def _start_watchdog() -> None:
    global _watchdog_task

    if _watchdog_task and not _watchdog_task.done():
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "Scheduler watchdog was not started because no event loop is running"
        )
        return

    _watchdog_task = loop.create_task(
        _scheduler_watchdog(),
        name="scheduler_runtime_watchdog",
    )


def start_scheduler() -> None:
    """Start APScheduler safely and register all regular jobs."""
    global _shutdown_requested, _started_at

    _shutdown_requested = False
    _ensure_listener()
    _register_core_jobs()

    if not scheduler.running:
        scheduler.start()
        _started_at = _started_at or _utcnow()
        logger.info(
            "Scheduler started jobs=%s",
            len(scheduler.get_jobs()),
        )

    _start_watchdog()


def restart_scheduler() -> bool:
    """Restart the scheduler without creating duplicate jobs."""
    global _last_restart_at, _restart_count, _shutdown_requested

    _shutdown_requested = False

    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)

        scheduler.start()
        _restore_registered_jobs()

        _restart_count += 1
        _last_restart_at = _utcnow()
        _start_watchdog()

        logger.info(
            "[RECOVERY] Scheduler manually restarted "
            "restart_count=%s jobs=%s",
            _restart_count,
            len(scheduler.get_jobs()),
        )
        return True
    except Exception:
        logger.exception("[RECOVERY] Scheduler manual restart failed")
        return False


def shutdown_scheduler() -> None:
    """Stop the scheduler and its watchdog during graceful shutdown."""
    global _shutdown_requested, _watchdog_task

    _shutdown_requested = True

    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = None

    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def scheduler_health() -> dict[str, Any]:
    """Return scheduler information for the runtime health endpoint."""
    now = _utcnow()
    uptime_seconds = (
        int((now - _started_at).total_seconds())
        if _started_at
        else 0
    )

    jobs = []
    try:
        jobs = scheduler.get_jobs()
    except Exception:
        logger.exception("Unable to read scheduler jobs")

    return {
        "running": bool(scheduler.running),
        "status": "running" if scheduler.running else "stopped",
        "jobs": len(jobs),
        "job_ids": sorted(job.id for job in jobs),
        "watchdog_running": bool(
            _watchdog_task and not _watchdog_task.done()
        ),
        "restart_count": _restart_count,
        "started_at": _iso(_started_at),
        "last_restart_at": _iso(_last_restart_at),
        "uptime_seconds": uptime_seconds,
    }


def add_interval_job(
    func,
    job_id: str,
    minutes: int,
    replace_existing: bool = True,
) -> None:
    definition = {
        "func": func,
        "trigger": "interval",
        "minutes": minutes,
        "id": job_id,
        "replace_existing": replace_existing,
    }
    _registered_jobs[job_id] = definition
    _register_job(definition)


def add_cron_job(func, job_id: str, **kwargs) -> None:
    definition = {
        "func": func,
        "trigger": "cron",
        "id": job_id,
        "replace_existing": True,
        **kwargs,
    }
    _registered_jobs[job_id] = definition
    _register_job(definition)
