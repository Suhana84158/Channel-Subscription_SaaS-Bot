from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Kolkata"


def get_zone(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((timezone_name or DEFAULT_TIMEZONE).strip())
    except (ZoneInfoNotFoundError, ValueError, AttributeError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_local_datetime(
    value: datetime | None,
    timezone_name: str | None,
    fmt: str = "%d-%m-%Y %I:%M %p",
) -> str:
    aware = ensure_utc(value)
    if aware is None:
        return "-"
    return aware.astimezone(get_zone(timezone_name)).strftime(fmt)
