import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE
from scheduler.expiry_worker import check_expired_users

logger = logging.getLogger(__name__)

_JOB_DEFAULTS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 120,
}

scheduler = AsyncIOScheduler(
    timezone=TIMEZONE,
    job_defaults=_JOB_DEFAULTS,
)

_listener_added = False
_watchdog_task: asyncio.Task | None = None
_shutdown_requested = False
_started_at: datetime | None = None
_last_restart_at: datetime | None = None
_restart_count = 0
_start_lock: asyncio.Lock | None = None

# Every job is kept here so startup and recovery can reconcile the scheduler
# without creating duplicates.
_registered_jobs: dict[str, dict[str, Any]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _get_start_lock() -> asyncio.Lock:
    global _start_lock
    if _start_lock is None:
        _start_lock = asyncio.Lock()
    return _start_lock


def _job_listener(event) -> None:
    if getattr(event, "exception", None):
        exc = event.exception
        logger.error(
            "Scheduler job failed job_id=%s",
            event.job_id,
            exc_info=(type(exc), exc, exc.__traceback__),
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
    **kwargs: Any,
) -> None:
    _registered_jobs[job_id] = {
        "func": func,
        "trigger": trigger,
        "id": job_id,
        "replace_existing": replace_existing,
        **kwargs,
    }


def _register_core_jobs() -> None:
    _remember_job(
        func=check_expired_users,
        trigger="interval",
        minutes=1,
        job_id="subscription_expiry_check",
        replace_existing=True,
    )


def _reconcile_jobs() -> None:
    """Create missing jobs and replace stale definitions atomically by ID."""
    configured_ids = set(_registered_jobs)

    for definition in _registered_jobs.values():
        scheduler.add_job(**definition)

    # Only remove jobs managed by this module. Unknown APScheduler jobs are
    # preserved so another component is not accidentally disrupted.
    existing_ids = {job.id for job in scheduler.get_jobs()}
    missing = configured_ids - existing_ids
    if missing:
        logger.error("Scheduler reconciliation left missing jobs=%s", sorted(missing))


def _start_scheduler_now() -> None:
    global _started_at

    _ensure_listener()
    _register_core_jobs()
    _reconcile_jobs()

    if not scheduler.running:
        scheduler.start()
        _started_at = _started_at or _utcnow()

    # A second reconciliation after start protects jobs added while the
    # scheduler was stopped and remains safe because all IDs replace existing.
    _reconcile_jobs()


def start_scheduler() -> None:
    """Start once and restore all registered jobs without duplication."""
    global _shutdown_requested
    _shutdown_requested = False
    _start_scheduler_now()
    _start_watchdog()
    logger.info("Scheduler ready jobs=%s", len(scheduler.get_jobs()))


def restart_scheduler() -> bool:
    """Recover a stopped scheduler and reconcile every registered job."""
    global _last_restart_at, _restart_count, _shutdown_requested
    _shutdown_requested = False

    try:
        _start_scheduler_now()
        _restart_count += 1
        _last_restart_at = _utcnow()
        _start_watchdog()
        logger.info(
            "[RECOVERY] Scheduler reconciled restart_count=%s jobs=%s",
            _restart_count,
            len(scheduler.get_jobs()),
        )
        return True
    except Exception:
        logger.exception("[RECOVERY] Scheduler recovery failed")
        return False


async def _scheduler_watchdog() -> None:
    logger.info("Scheduler watchdog started")

    while not _shutdown_requested:
        try:
            await asyncio.sleep(60)
            if _shutdown_requested:
                break

            # Reconcile even while running. This restores any job that was
            # accidentally removed at runtime and replaces stale definitions.
            async with _get_start_lock():
                if not scheduler.running:
                    logger.warning("[RECOVERY] Scheduler stopped unexpectedly")
                    restart_scheduler()
                else:
                    _reconcile_jobs()
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
        logger.warning("Scheduler watchdog requires a running event loop")
        return

    _watchdog_task = loop.create_task(
        _scheduler_watchdog(),
        name="scheduler_runtime_watchdog",
    )


def shutdown_scheduler() -> None:
    """Stop background scheduling during graceful application shutdown."""
    global _shutdown_requested, _watchdog_task
    _shutdown_requested = True

    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = None

    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def scheduler_health() -> dict[str, Any]:
    now = _utcnow()
    uptime_seconds = (
        int((now - _started_at).total_seconds()) if _started_at else 0
    )

    try:
        jobs = scheduler.get_jobs()
    except Exception:
        logger.exception("Unable to read scheduler jobs")
        jobs = []

    configured_ids = sorted(_registered_jobs)
    active_ids = sorted(job.id for job in jobs)

    return {
        "running": bool(scheduler.running),
        "status": "running" if scheduler.running else "stopped",
        "jobs": len(jobs),
        "job_ids": active_ids,
        "configured_job_ids": configured_ids,
        "missing_job_ids": sorted(set(configured_ids) - set(active_ids)),
        "watchdog_running": bool(
            _watchdog_task and not _watchdog_task.done()
        ),
        "restart_count": _restart_count,
        "started_at": _iso(_started_at),
        "last_restart_at": _iso(_last_restart_at),
        "uptime_seconds": uptime_seconds,
    }


def add_interval_job(
    func: Callable,
    job_id: str,
    minutes: int,
    replace_existing: bool = True,
) -> None:
    if minutes <= 0:
        raise ValueError("minutes must be greater than zero")

    _remember_job(
        func=func,
        trigger="interval",
        minutes=minutes,
        job_id=job_id,
        replace_existing=replace_existing,
    )

    if scheduler.running:
        _reconcile_jobs()


def add_cron_job(func: Callable, job_id: str, **kwargs: Any) -> None:
    _remember_job(
        func=func,
        trigger="cron",
        job_id=job_id,
        replace_existing=True,
        **kwargs,
    )

    if scheduler.running:
        _reconcile_jobs()
