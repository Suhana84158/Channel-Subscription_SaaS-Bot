from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from database.mongo import get_database


COLLECTION = "seller_deleting_message_settings"
STATS_COLLECTION = "seller_deleting_message_stats"


DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,

    # Delete Commands
    "delete_commands": {
        "admins": False,
        "users": False,
        "prefixes": ["/"],
    },

    # Link Protection
    "link_protection": {
        "enabled": False,
        "all_links": True,
        "telegram": True,
        "instagram": True,
        "youtube": True,
        "facebook": True,
        "x_twitter": True,
        "tiktok": True,
        "discord": True,
        "custom_domains": [],
    },

    # Forwarded Media
    "forwarded_media": {
        "enabled": False,
        "photo": True,
        "video": True,
        "animation": True,
        "document": True,
        "audio": True,
        "voice": True,
        "sticker": True,
        "video_note": True,
    },

    # Service Messages
    "service_messages": {
        "join": False,
        "exit": False,
        "photos": False,
        "title": False,
        "pinned": False,
        "topic": False,
        "boost": False,
        "video_chats": False,
        "checklist": False,
        "community": False,
    },

    # Safety
    "ignore_admins": True,
    "ignore_owner": True,
    "whitelisted_user_ids": [],

    # Delete All Messages
    "delete_all": {
        "last_message_id": 0,
        "last_chat_id": 0,
    },
}


STAT_FIELDS = {
    "commands_deleted",
    "links_deleted",
    "forwarded_media_deleted",
    "service_messages_deleted",
    "delete_all_deleted",
    "total_deleted",
    "failed_deletions",
}


def collection():
    return get_database()[COLLECTION]


def stats_collection():
    return get_database()[STATS_COLLECTION]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def deep_merge(defaults: Dict[str, Any], saved: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(defaults)

    for key, value in (saved or {}).items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result


async def initialize_deleting_message_indexes() -> None:
    await collection().create_index(
        [("owner_id", 1)],
        unique=True,
    )

    await stats_collection().create_index(
        [
            ("owner_id", 1),
            ("date_key", 1),
        ],
        unique=True,
    )

    await stats_collection().create_index(
        [
            ("owner_id", 1),
            ("created_at", -1),
        ]
    )


async def ensure_deleting_message_settings(owner_id: int) -> Dict[str, Any]:
    owner_id = int(owner_id)
    now = utc_now()

    document = {
        "owner_id": owner_id,
        **deepcopy(DEFAULT_SETTINGS),
        "created_at": now,
        "updated_at": now,
    }

    await collection().update_one(
        {"owner_id": owner_id},
        {"$setOnInsert": document},
        upsert=True,
    )

    saved = await collection().find_one(
        {"owner_id": owner_id}
    ) or {}

    merged = deep_merge(DEFAULT_SETTINGS, saved)
    merged["owner_id"] = owner_id
    merged["created_at"] = saved.get("created_at", now)
    merged["updated_at"] = saved.get("updated_at", now)

    missing_update: Dict[str, Any] = {}

    for key, value in DEFAULT_SETTINGS.items():
        if key not in saved:
            missing_update[key] = deepcopy(value)
        elif isinstance(value, dict):
            merged_section = deep_merge(
                value,
                saved.get(key) or {},
            )
            if merged_section != saved.get(key):
                missing_update[key] = merged_section

    if missing_update:
        missing_update["updated_at"] = now

        await collection().update_one(
            {"owner_id": owner_id},
            {"$set": missing_update},
        )

        merged.update(missing_update)

    return merged


async def get_deleting_message_settings(
    owner_id: int,
) -> Dict[str, Any]:
    return await ensure_deleting_message_settings(owner_id)


async def set_module_enabled(
    owner_id: int,
    enabled: bool,
) -> Dict[str, Any]:
    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": {
                "enabled": bool(enabled),
                "updated_at": utc_now(),
            },
            "$setOnInsert": {
                "created_at": utc_now(),
            },
        },
        upsert=True,
    )

    return await get_deleting_message_settings(owner_id)


async def update_section(
    owner_id: int,
    section: str,
    values: Dict[str, Any],
) -> Dict[str, Any]:
    if section not in DEFAULT_SETTINGS:
        raise ValueError("Unknown deleting-messages section.")

    if not isinstance(DEFAULT_SETTINGS[section], dict):
        raise ValueError("This setting is not a section.")

    if not isinstance(values, dict):
        raise ValueError("Section values must be a dictionary.")

    allowed_keys = set(DEFAULT_SETTINGS[section])
    invalid_keys = set(values) - allowed_keys

    if invalid_keys:
        raise ValueError(
            "Unsupported setting(s): "
            + ", ".join(sorted(invalid_keys))
        )

    update_values = {
        f"{section}.{key}": value
        for key, value in values.items()
    }
    update_values["updated_at"] = utc_now()

    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": update_values,
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "created_at": utc_now(),
            },
        },
        upsert=True,
    )

    return await get_deleting_message_settings(owner_id)


async def set_section_value(
    owner_id: int,
    section: str,
    key: str,
    value: Any,
) -> Dict[str, Any]:
    return await update_section(
        owner_id,
        section,
        {key: value},
    )


async def toggle_section_value(
    owner_id: int,
    section: str,
    key: str,
) -> Dict[str, Any]:
    settings = await get_deleting_message_settings(owner_id)

    if section not in settings:
        raise ValueError("Unknown deleting-messages section.")

    current_section = settings.get(section)

    if not isinstance(current_section, dict):
        raise ValueError("This setting is not a section.")

    if key not in current_section:
        raise ValueError("Unknown setting key.")

    current_value = current_section[key]

    if not isinstance(current_value, bool):
        raise ValueError("Only boolean settings can be toggled.")

    return await set_section_value(
        owner_id,
        section,
        key,
        not current_value,
    )


async def set_delete_command_mode(
    owner_id: int,
    *,
    admins: Optional[bool] = None,
    users: Optional[bool] = None,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {}

    if admins is not None:
        values["admins"] = bool(admins)

    if users is not None:
        values["users"] = bool(users)

    if not values:
        return await get_deleting_message_settings(owner_id)

    return await update_section(
        owner_id,
        "delete_commands",
        values,
    )


async def set_command_prefixes(
    owner_id: int,
    prefixes: Iterable[str],
) -> Dict[str, Any]:
    cleaned = []

    for prefix in prefixes:
        prefix = str(prefix or "").strip()

        if not prefix:
            continue

        if len(prefix) > 3:
            raise ValueError(
                "Each command prefix must be 1 to 3 characters."
            )

        if prefix not in cleaned:
            cleaned.append(prefix)

    if not cleaned:
        cleaned = ["/"]

    return await set_section_value(
        owner_id,
        "delete_commands",
        "prefixes",
        cleaned,
    )


async def set_link_protection_enabled(
    owner_id: int,
    enabled: bool,
) -> Dict[str, Any]:
    return await set_section_value(
        owner_id,
        "link_protection",
        "enabled",
        bool(enabled),
    )


async def set_forwarded_media_enabled(
    owner_id: int,
    enabled: bool,
) -> Dict[str, Any]:
    return await set_section_value(
        owner_id,
        "forwarded_media",
        "enabled",
        bool(enabled),
    )


def normalize_domain(domain: str) -> str:
    value = str(domain or "").strip().lower()

    value = value.removeprefix("https://")
    value = value.removeprefix("http://")
    value = value.removeprefix("www.")
    value = value.split("/", 1)[0]
    value = value.strip(".")

    if not value or "." not in value:
        raise ValueError(
            "Send a valid domain, for example example.com"
        )

    if " " in value:
        raise ValueError("Domain cannot contain spaces.")

    return value


async def add_custom_domain(
    owner_id: int,
    domain: str,
) -> Dict[str, Any]:
    settings = await get_deleting_message_settings(owner_id)
    domains = list(
        settings
        .get("link_protection", {})
        .get("custom_domains", [])
    )

    normalized = normalize_domain(domain)

    if normalized not in domains:
        domains.append(normalized)

    domains = sorted(set(domains))

    return await set_section_value(
        owner_id,
        "link_protection",
        "custom_domains",
        domains,
    )


async def remove_custom_domain(
    owner_id: int,
    domain: str,
) -> Dict[str, Any]:
    settings = await get_deleting_message_settings(owner_id)
    domains = list(
        settings
        .get("link_protection", {})
        .get("custom_domains", [])
    )

    normalized = normalize_domain(domain)

    domains = [
        item
        for item in domains
        if item != normalized
    ]

    return await set_section_value(
        owner_id,
        "link_protection",
        "custom_domains",
        domains,
    )


async def clear_custom_domains(
    owner_id: int,
) -> Dict[str, Any]:
    return await set_section_value(
        owner_id,
        "link_protection",
        "custom_domains",
        [],
    )


async def set_ignore_admins(
    owner_id: int,
    enabled: bool,
) -> Dict[str, Any]:
    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": {
                "ignore_admins": bool(enabled),
                "updated_at": utc_now(),
            },
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "created_at": utc_now(),
            },
        },
        upsert=True,
    )

    return await get_deleting_message_settings(owner_id)


async def set_ignore_owner(
    owner_id: int,
    enabled: bool,
) -> Dict[str, Any]:
    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": {
                "ignore_owner": bool(enabled),
                "updated_at": utc_now(),
            },
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "created_at": utc_now(),
            },
        },
        upsert=True,
    )

    return await get_deleting_message_settings(owner_id)


async def add_whitelisted_user(
    owner_id: int,
    user_id: int,
) -> Dict[str, Any]:
    await ensure_deleting_message_settings(owner_id)

    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$addToSet": {
                "whitelisted_user_ids": int(user_id),
            },
            "$set": {
                "updated_at": utc_now(),
            },
        },
    )

    return await get_deleting_message_settings(owner_id)


async def remove_whitelisted_user(
    owner_id: int,
    user_id: int,
) -> Dict[str, Any]:
    await ensure_deleting_message_settings(owner_id)

    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$pull": {
                "whitelisted_user_ids": int(user_id),
            },
            "$set": {
                "updated_at": utc_now(),
            },
        },
    )

    return await get_deleting_message_settings(owner_id)


async def clear_whitelisted_users(
    owner_id: int,
) -> Dict[str, Any]:
    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": {
                "whitelisted_user_ids": [],
                "updated_at": utc_now(),
            },
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "created_at": utc_now(),
            },
        },
        upsert=True,
    )

    return await get_deleting_message_settings(owner_id)


async def save_delete_all_position(
    owner_id: int,
    chat_id: int,
    last_message_id: int,
) -> Dict[str, Any]:
    return await update_section(
        owner_id,
        "delete_all",
        {
            "last_chat_id": int(chat_id),
            "last_message_id": int(last_message_id),
        },
    )


async def reset_delete_all_position(
    owner_id: int,
) -> Dict[str, Any]:
    return await update_section(
        owner_id,
        "delete_all",
        {
            "last_chat_id": 0,
            "last_message_id": 0,
        },
    )


def current_date_key() -> str:
    return utc_now().strftime("%Y-%m-%d")


async def increment_deletion_stat(
    owner_id: int,
    stat_name: str,
    amount: int = 1,
) -> None:
    if stat_name not in STAT_FIELDS:
        raise ValueError("Unknown deletion statistic.")

    amount = int(amount)

    if amount <= 0:
        return

    now = utc_now()
    date_key = now.strftime("%Y-%m-%d")

    increments = {
        stat_name: amount,
    }

    if stat_name != "total_deleted":
        increments["total_deleted"] = amount

    await stats_collection().update_one(
        {
            "owner_id": int(owner_id),
            "date_key": date_key,
        },
        {
            "$inc": increments,
            "$set": {
                "updated_at": now,
            },
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "date_key": date_key,
                "created_at": now,
            },
        },
        upsert=True,
    )


async def get_today_deletion_stats(
    owner_id: int,
) -> Dict[str, Any]:
    document = await stats_collection().find_one(
        {
            "owner_id": int(owner_id),
            "date_key": current_date_key(),
        }
    ) or {}

    result = {
        field: int(document.get(field, 0) or 0)
        for field in STAT_FIELDS
    }

    result["date_key"] = current_date_key()
    return result


async def get_deletion_stats_summary(
    owner_id: int,
    days: int = 30,
) -> Dict[str, Any]:
    days = max(1, min(int(days), 365))

    rows = await stats_collection().find(
        {"owner_id": int(owner_id)}
    ).sort(
        "date_key",
        -1,
    ).limit(
        days,
    ).to_list(
        length=days,
    )

    summary = {
        field: 0
        for field in STAT_FIELDS
    }

    for row in rows:
        for field in STAT_FIELDS:
            summary[field] += int(
                row.get(field, 0) or 0
            )

    summary["days"] = days
    summary["records"] = len(rows)
    return summary


async def reset_deleting_message_settings(
    owner_id: int,
) -> Dict[str, Any]:
    now = utc_now()

    document = {
        "owner_id": int(owner_id),
        **deepcopy(DEFAULT_SETTINGS),
        "created_at": now,
        "updated_at": now,
    }

    await collection().replace_one(
        {"owner_id": int(owner_id)},
        document,
        upsert=True,
    )

    return document
