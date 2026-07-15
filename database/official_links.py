from database.settings import get_setting_value, set_setting

KEY_CHANNEL = "official_channel_url"
KEY_GROUP = "official_group_url"
KEY_SUPPORT = "official_support_url"


def normalize_telegram_link(value: str) -> str:
    value=(value or "").strip()
    if not value:
        return ""
    if value.startswith("@"):
        return "https://t.me/"+value[1:]
    if value.startswith("t.me/"):
        return "https://"+value
    if value.startswith("telegram.me/"):
        return "https://"+value
    if value.startswith(("https://t.me/","http://t.me/","tg://")):
        return value
    raise ValueError("Send @username or a Telegram link such as https://t.me/example")


async def get_official_links():
    return {
        "channel": await get_setting_value(KEY_CHANNEL, ""),
        "group": await get_setting_value(KEY_GROUP, ""),
        "support": await get_setting_value(KEY_SUPPORT, ""),
    }


async def set_official_link(kind: str, value: str):
    key={"channel":KEY_CHANNEL,"group":KEY_GROUP,"support":KEY_SUPPORT}.get(kind)
    if not key:
        raise ValueError("Unknown official link type")
    normalized=normalize_telegram_link(value) if value else ""
    await set_setting(key, normalized)
    return normalized
