import asyncio

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from config import DATABASE_NAME, MONGO_URI
from logging_config import get_logger

logger = get_logger(__name__)

client = None
db = None
_connect_lock = asyncio.Lock()


async def connect_database(max_attempts: int = 5):
    """Connect to MongoDB with bounded retries and production-safe timeouts."""
    global client, db

    if db is not None:
        return db

    async with _connect_lock:
        if db is not None:
            return db

        last_error = None
        for attempt in range(1, max_attempts + 1):
            candidate = None
            try:
                candidate = AsyncIOMotorClient(
                    MONGO_URI,
                    serverSelectionTimeoutMS=8_000,
                    connectTimeoutMS=8_000,
                    socketTimeoutMS=20_000,
                    maxPoolSize=40,
                    minPoolSize=0,
                    maxIdleTimeMS=60_000,
                    retryReads=True,
                    retryWrites=True,
                    appname="telegram-subscription-saas-bot",
                )
                await candidate.admin.command("ping")
                client = candidate
                db = client[DATABASE_NAME]
                logger.info("MongoDB connected successfully (attempt %s).", attempt)
                return db
            except PyMongoError as exc:
                last_error = exc
                if candidate is not None:
                    candidate.close()
                delay = min(2 ** (attempt - 1), 15)
                logger.warning(
                    "MongoDB connection attempt %s/%s failed: %s. Retrying in %ss.",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)

        logger.error("MongoDB connection failed after %s attempts.", max_attempts)
        raise RuntimeError("Unable to connect to MongoDB") from last_error


def get_database():
    if db is None:
        raise RuntimeError("Database is not initialized.")
    return db


def is_connected():
    return db is not None


async def ping_database(timeout: float = 5.0) -> bool:
    if client is None:
        return False
    try:
        await asyncio.wait_for(client.admin.command("ping"), timeout=timeout)
        return True
    except Exception:
        logger.warning("MongoDB health ping failed.", exc_info=True)
        return False


async def close_database():
    global client, db
    if client is not None:
        client.close()
    client = None
    db = None
    logger.info("MongoDB connection closed.")
