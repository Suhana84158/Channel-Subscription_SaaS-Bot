from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConnectionFailure

from config import MONGO_URI, DATABASE_NAME
from logging_config import get_logger

logger = get_logger(__name__)

client = None
db = None


async def connect_database():
    """
    Connect to MongoDB.
    """

    global client, db

    try:
        client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
        )

        await client.admin.command("ping")

        db = client[DATABASE_NAME]

        logger.info("MongoDB connected successfully.")

    except ConnectionFailure as e:
        logger.exception("MongoDB connection failed.")
        raise e


def get_database():
    """
    Return database instance.
    """

    if db is None:
        raise RuntimeError(
            "Database is not initialized."
        )

    return db


def is_connected():
    """
    Check whether database is initialized.
    """

    return db is not None


async def close_database():
    """
    Close MongoDB connection.
    """

    global client

    if client:
        client.close()
        logger.info("MongoDB connection closed.")
