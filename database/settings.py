import asyncio
import time
from datetime import datetime, timezone

from database.mongo import get_database

COLLECTION = "settings"


DEFAULT_SETTINGS = {
    "bot_name": "Subscription Bot",
    "welcome_message": (
        "👋 Welcome to Subscription Bot!\n\n"
        "Choose an option from the menu below."
    ),
    "support_username": "",
    "upi_id": "",
    "upi_name": "",
    "upi_qr_file_id": None,
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "language": "en",
    "auto_remove": True,
    "reminder_days": 1,
    "maintenance_mode": False,
    "official_channel_url": "",
    "official_group_url": "",
    "official_support_url": "",
}


def settings_collection():
    return get_database()[COLLECTION]


_CACHE_TTL_SECONDS = 15.0
_settings_cache: dict[str, object] | None = None
_settings_cache_at = 0.0
_cache_lock = asyncio.Lock()


def _invalidate_cache():
    global _settings_cache, _settings_cache_at
    _settings_cache = None
    _settings_cache_at = 0.0


async def get_setting(key: str):
    return await settings_collection().find_one(
        {"key": key}
    )


async def get_setting_value(key: str, default=None):
    settings = await get_all_settings()
    return settings.get(key, default)


async def set_setting(key: str, value):
    now = datetime.now(timezone.utc)

    await settings_collection().update_one(
        {"key": key},
        {
            "$set": {
                "value": value,
                "updated_at": now,
            },
            "$setOnInsert": {
                "created_at": now,
            },
        },
        upsert=True,
    )
    _invalidate_cache()


async def set_multiple_settings(settings: dict):
    await asyncio.gather(*(set_setting(key, value) for key, value in settings.items()))


async def delete_setting(key: str):
    await settings_collection().delete_one(
        {"key": key}
    )
    _invalidate_cache()


async def get_all_settings(force_refresh: bool = False):
    global _settings_cache, _settings_cache_at
    now = time.monotonic()
    if (
        not force_refresh
        and _settings_cache is not None
        and now - _settings_cache_at < _CACHE_TTL_SECONDS
    ):
        return dict(_settings_cache)

    async with _cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _settings_cache is not None
            and now - _settings_cache_at < _CACHE_TTL_SECONDS
        ):
            return dict(_settings_cache)

        documents = await settings_collection().find().to_list(length=None)
        saved_settings = {
            document["key"]: document.get("value")
            for document in documents
        }
        result = DEFAULT_SETTINGS.copy()
        result.update(saved_settings)
        _settings_cache = dict(result)
        _settings_cache_at = now
        return dict(result)


async def initialize_default_settings():
    for key, value in DEFAULT_SETTINGS.items():
        existing = await get_setting(key)

        if existing is None:
            await set_setting(key, value)


async def maintenance_mode() -> bool:
    value = await get_setting_value(
        "maintenance_mode",
        False,
    )

    return bool(value)
