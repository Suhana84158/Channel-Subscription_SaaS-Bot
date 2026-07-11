import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LOG_LEVEL

# Create logs directory
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / "bot.log"

LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | "
    "%(name)s | %(filename)s:%(lineno)d | %(message)s"
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    logger.handlers.clear()

    formatter = logging.Formatter(
        LOG_FORMAT,
        DATE_FORMAT
    )

    # Console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logging.info("Logging initialized successfully.")


def get_logger(name: str):
    return logging.getLogger(name)
