import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from config import DATABASE_NAME, MONGO_URI

logger = logging.getLogger(__name__)

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None

_connect_lock = asyncio.Lock()
_connected_at: datetime | None = None
_last_ping_at: datetime | None = None
_last_ping_ok: bool = False


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


def _reset_connection() -> None:
    global client, db, _connected_at, _last_ping_at, _last_ping_ok

    old_client = client
    client = None
    db = None
    _connected_at = None
    _last_ping_at = None
    _last_ping_ok = False

    if old_client is not None:
        try:
            old_client.close()
        except Exception:
            logger.exception("Failed to close the old MongoDB client.")


async def connect_database(max_attempts: int = 5) -> AsyncIOMotorDatabase:
    """Connect to MongoDB with bounded retries and production-safe timeouts."""
    global client, db, _connected_at, _last_ping_at, _last_ping_ok

    _validate_database_config()

    if db is not None and await ping_database(timeout=5, log_failure=False):
        return db

    async with _connect_lock:
        if db is not None and await ping_database(timeout=5, log_failure=False):
            return db

        # A stale/dead client must not be reused by later startup attempts.
        _reset_connection()

        last_error: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            candidate: AsyncIOMotorClient | None = None

            try:
                logger.info(
                    "Connecting to MongoDB attempt=%s/%s database=%s",
                    attempt,
                    max_attempts,
                    DATABASE_NAME,
                )

                candidate = _create_client()
                await asyncio.wait_for(
                    candidate.admin.command("ping"),
                    timeout=10,
                )

                client = candidate
                db = candidate[DATABASE_NAME]
                now = datetime.now(timezone.utc)
                _connected_at = now
                _last_ping_at = now
                _last_ping_ok = True

                logger.info(
                    "MongoDB connected successfully attempt=%s database=%s",
                    attempt,
                    DATABASE_NAME,
                )
                return db

            except asyncio.CancelledError:
                if candidate is not None:
                    candidate.close()
                raise

            except (PyMongoError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc

                if candidate is not None:
                    candidate.close()

                delay = min(2 ** (attempt - 1), 15)
                logger.warning(
                    "MongoDB connection failed attempt=%s/%s error=%s "
                    "retry_in_seconds=%s",
                    attempt,
                    max_attempts,
                    exc,
                    delay if attempt < max_attempts else 0,
                )

                if attempt < max_attempts:
                    await asyncio.sleep(delay)

            except Exception as exc:
                last_error = exc

                if candidate is not None:
                    candidate.close()

                logger.exception(
                    "Unexpected MongoDB startup error attempt=%s/%s",
                    attempt,
                    max_attempts,
                )

                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 15))

        _reset_connection()
        logger.error(
            "MongoDB connection failed after attempts=%s database=%s",
            max_attempts,
            DATABASE_NAME,
        )
        raise RuntimeError("Unable to connect to MongoDB.") from last_error


def get_database() -> AsyncIOMotorDatabase:
    """
    Return the initialized database object.

    This remains synchronous for compatibility with existing database modules.
    Startup code must call connect_database() before handlers begin.
    """
    if db is None:
        raise RuntimeError(
            "Database is not initialized. Call connect_database() first."
        )
    return db


def is_connected() -> bool:
    """Return the last known connection state without performing network I/O."""
    return db is not None and client is not None and _last_ping_ok


async def ping_database(
    timeout: float = 5.0,
    *,
    log_failure: bool = True,
) -> bool:
    """Perform a bounded MongoDB health ping."""
    global _last_ping_at, _last_ping_ok

    current_client = client
    _last_ping_at = datetime.now(timezone.utc)

    if current_client is None:
        _last_ping_ok = False
        return False

    try:
        await asyncio.wait_for(
            current_client.admin.command("ping"),
            timeout=timeout,
        )
        _last_ping_ok = True
        return True

    except asyncio.CancelledError:
        raise

    except Exception:
        _last_ping_ok = False

        if log_failure:
            logger.warning("MongoDB health ping failed.", exc_info=True)

        return False


async def ensure_database(
    *,
    max_attempts: int = 3,
    ping_timeout: float = 5.0,
) -> AsyncIOMotorDatabase:
    """
    Return a healthy database connection, reconnecting when the cached client
    is unavailable. Existing modules can adopt this helper gradually.
    """
    if db is not None and await ping_database(
        timeout=ping_timeout,
        log_failure=False,
    ):
        return db

    logger.warning("MongoDB connection is unhealthy; reconnecting.")
    return await connect_database(max_attempts=max_attempts)


def get_database_health() -> dict[str, Any]:
    """Return lightweight connection metadata for the health endpoint."""
    return {
        "configured": bool(MONGO_URI and DATABASE_NAME),
        "initialized": db is not None and client is not None,
        "last_ping_ok": _last_ping_ok,
        "connected_at": (
            _connected_at.isoformat() if _connected_at is not None else None
        ),
        "last_ping_at": (
            _last_ping_at.isoformat() if _last_ping_at is not None else None
        ),
        "database_name": DATABASE_NAME or None,
    }


async def close_database() -> None:
    """Close MongoDB safely and clear all cached connection state."""
    async with _connect_lock:
        was_initialized = client is not None or db is not None
        _reset_connection()

    if was_initialized:
        logger.info("MongoDB connection closed.")
    else:
        logger.info("MongoDB connection was already closed.")
