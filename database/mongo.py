import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from config import DATABASE_NAME, MONGO_URI

logger = logging.getLogger(__name__)

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None

_connect_lock = asyncio.Lock()
_watchdog_task: asyncio.Task | None = None
_shutdown_requested = False

_connected_at: datetime | None = None
_last_ping_at: datetime | None = None
_last_reconnect_at: datetime | None = None
_last_ping_ok = False
_last_latency_ms: float | None = None
_reconnect_count = 0
_consecutive_failures = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _validate_database_config() -> None:
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing.")

    if not DATABASE_NAME:
        raise RuntimeError("DATABASE_NAME is missing.")

    if not (
        MONGO_URI.startswith("mongodb://")
        or MONGO_URI.startswith("mongodb+srv://")
    ):
        raise RuntimeError("MONGO_URI format is invalid.")


def _create_client() -> AsyncIOMotorClient:
    return AsyncIOMotorClient(
        MONGO_URI,
        serverSelectionTimeoutMS=8_000,
        connectTimeoutMS=8_000,
        socketTimeoutMS=20_000,
        waitQueueTimeoutMS=10_000,
        maxPoolSize=40,
        minPoolSize=0,
        maxIdleTimeMS=60_000,
        heartbeatFrequencyMS=10_000,
        retryReads=True,
        retryWrites=True,
        appname="telegram-subscription-saas-bot",
    )


def _close_client(old_client: AsyncIOMotorClient | None) -> None:
    if old_client is None:
        return

    try:
        old_client.close()
    except Exception:
        logger.exception("Failed to close MongoDB client.")


def _reset_connection() -> None:
    global client, db, _connected_at, _last_ping_ok, _last_latency_ms

    old_client = client
    client = None
    db = None
    _connected_at = None
    _last_ping_ok = False
    _last_latency_ms = None
    _close_client(old_client)


async def ping_database(
    timeout: float = 5.0,
    *,
    log_failure: bool = True,
) -> bool:
    """Perform a bounded MongoDB health ping and record latency."""
    global _last_ping_at, _last_ping_ok, _last_latency_ms
    global _consecutive_failures

    current_client = client
    _last_ping_at = _utcnow()

    if current_client is None:
        _last_ping_ok = False
        _last_latency_ms = None
        _consecutive_failures += 1
        return False

    started = time.perf_counter()

    try:
        await asyncio.wait_for(
            current_client.admin.command("ping"),
            timeout=timeout,
        )
        _last_latency_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )
        _last_ping_ok = True
        _consecutive_failures = 0
        return True

    except asyncio.CancelledError:
        raise

    except Exception:
        _last_ping_ok = False
        _last_latency_ms = None
        _consecutive_failures += 1

        if log_failure:
            logger.warning(
                "[MONGO] Health ping failed consecutive_failures=%s",
                _consecutive_failures,
                exc_info=True,
            )

        return False


async def connect_database(
    max_attempts: int = 5,
) -> AsyncIOMotorDatabase:
    """Connect or reconnect to MongoDB with bounded exponential retries."""
    global client, db, _connected_at, _last_ping_at, _last_ping_ok
    global _last_latency_ms, _last_reconnect_at, _reconnect_count
    global _shutdown_requested

    _validate_database_config()
    _shutdown_requested = False

    if db is not None and await ping_database(
        timeout=5,
        log_failure=False,
    ):
        _start_watchdog()
        return db

    async with _connect_lock:
        if db is not None and await ping_database(
            timeout=5,
            log_failure=False,
        ):
            _start_watchdog()
            return db

        was_reconnect = client is not None or db is not None
        _reset_connection()

        delays = (2, 5, 10, 30)
        last_error: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            candidate: AsyncIOMotorClient | None = None

            try:
                logger.info(
                    "[MONGO] Connecting attempt=%s/%s database=%s",
                    attempt,
                    max_attempts,
                    DATABASE_NAME,
                )

                candidate = _create_client()
                started = time.perf_counter()

                await asyncio.wait_for(
                    candidate.admin.command("ping"),
                    timeout=10,
                )

                now = _utcnow()
                client = candidate
                db = candidate[DATABASE_NAME]
                _connected_at = now
                _last_ping_at = now
                _last_ping_ok = True
                _last_latency_ms = round(
                    (time.perf_counter() - started) * 1000,
                    2,
                )

                if was_reconnect or _reconnect_count:
                    _reconnect_count += 1
                    _last_reconnect_at = now
                    logger.info(
                        "[MONGO] Reconnected successfully "
                        "latency_ms=%s reconnect_count=%s",
                        _last_latency_ms,
                        _reconnect_count,
                    )
                else:
                    logger.info(
                        "[MONGO] Connected successfully latency_ms=%s",
                        _last_latency_ms,
                    )

                _start_watchdog()
                return db

            except asyncio.CancelledError:
                _close_client(candidate)
                raise

            except (PyMongoError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                _close_client(candidate)

                delay = delays[min(attempt - 1, len(delays) - 1)]
                logger.warning(
                    "[MONGO] Connection failed attempt=%s/%s "
                    "retry_in_seconds=%s error=%s",
                    attempt,
                    max_attempts,
                    delay if attempt < max_attempts else 0,
                    exc,
                )

                if attempt < max_attempts:
                    await asyncio.sleep(delay)

            except Exception as exc:
                last_error = exc
                _close_client(candidate)
                logger.exception(
                    "[MONGO] Unexpected connection error attempt=%s/%s",
                    attempt,
                    max_attempts,
                )

                if attempt < max_attempts:
                    await asyncio.sleep(
                        delays[min(attempt - 1, len(delays) - 1)]
                    )

        _reset_connection()
        raise RuntimeError(
            "Unable to connect to MongoDB."
        ) from last_error


async def ensure_database(
    *,
    max_attempts: int = 3,
    ping_timeout: float = 5.0,
) -> AsyncIOMotorDatabase:
    """Return a healthy database and reconnect when required."""
    if db is not None and await ping_database(
        timeout=ping_timeout,
        log_failure=False,
    ):
        return db

    logger.warning("[MONGO] Connection unhealthy; reconnecting.")
    return await connect_database(max_attempts=max_attempts)


async def _database_watchdog() -> None:
    """Continuously verify MongoDB and recover a lost connection."""
    logger.info("[MONGO] Runtime watchdog started.")

    while not _shutdown_requested:
        try:
            await asyncio.sleep(30)

            if _shutdown_requested:
                break

            if await ping_database(timeout=5, log_failure=False):
                continue

            logger.warning(
                "[MONGO] Connection lost; automatic recovery started."
            )

            try:
                await connect_database(max_attempts=4)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[MONGO] Automatic recovery failed; "
                    "watchdog will retry after 30 seconds."
                )

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[MONGO] Watchdog cycle failed.")

    logger.info("[MONGO] Runtime watchdog stopped.")


def _start_watchdog() -> None:
    global _watchdog_task

    if _shutdown_requested:
        return

    if _watchdog_task and not _watchdog_task.done():
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "[MONGO] Watchdog not started because no event loop is running."
        )
        return

    _watchdog_task = loop.create_task(
        _database_watchdog(),
        name="mongodb_runtime_watchdog",
    )


def get_database() -> AsyncIOMotorDatabase:
    """Return the initialized database object."""
    if db is None:
        raise RuntimeError(
            "Database is not initialized. Call connect_database() first."
        )
    return db


def is_connected() -> bool:
    return bool(
        db is not None
        and client is not None
        and _last_ping_ok
    )


def get_database_health() -> dict[str, Any]:
    """Return connection metadata for /health and monitoring."""
    return {
        "configured": bool(MONGO_URI and DATABASE_NAME),
        "initialized": db is not None and client is not None,
        "status": "connected" if is_connected() else "disconnected",
        "last_ping_ok": _last_ping_ok,
        "latency_ms": _last_latency_ms,
        "connected_at": _iso(_connected_at),
        "last_ping_at": _iso(_last_ping_at),
        "last_reconnect_at": _iso(_last_reconnect_at),
        "reconnect_count": _reconnect_count,
        "consecutive_failures": _consecutive_failures,
        "watchdog_running": bool(
            _watchdog_task and not _watchdog_task.done()
        ),
        "database_name": DATABASE_NAME or None,
    }


async def close_database() -> None:
    """Stop the watchdog and close MongoDB safely."""
    global _shutdown_requested, _watchdog_task

    _shutdown_requested = True

    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[MONGO] Watchdog shutdown failed.")

    _watchdog_task = None

    async with _connect_lock:
        was_initialized = client is not None or db is not None
        _reset_connection()

    if was_initialized:
        logger.info("[MONGO] Connection closed.")
