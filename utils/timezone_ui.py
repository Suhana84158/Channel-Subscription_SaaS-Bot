from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TIMEZONE_CHOICES = {
    "india": ("🇮🇳 India", "Asia/Kolkata"),
    "bangladesh": ("🇧🇩 Bangladesh", "Asia/Dhaka"),
    "nepal": ("🇳🇵 Nepal", "Asia/Kathmandu"),
    "pakistan": ("🇵🇰 Pakistan", "Asia/Karachi"),
    "uae": ("🇦🇪 UAE", "Asia/Dubai"),
    "saudi": ("🇸🇦 Saudi Arabia", "Asia/Riyadh"),
    "thailand": ("🇹🇭 Thailand", "Asia/Bangkok"),
    "singapore": ("🇸🇬 Singapore", "Asia/Singapore"),
    "malaysia": ("🇲🇾 Malaysia", "Asia/Kuala_Lumpur"),
    "indonesia": ("🇮🇩 Indonesia", "Asia/Jakarta"),
    "philippines": ("🇵🇭 Philippines", "Asia/Manila"),
    "uk": ("🇬🇧 United Kingdom", "Europe/London"),
    "germany": ("🇩🇪 Germany", "Europe/Berlin"),
    "us_east": ("🇺🇸 USA – New York", "America/New_York"),
    "us_west": ("🇺🇸 USA – Los Angeles", "America/Los_Angeles"),
    "australia": ("🇦🇺 Australia – Sydney", "Australia/Sydney"),
}


def timezone_guide(current: str | None = None) -> str:
    current_line = f"\nCurrent timezone: {current}" if current else ""
    return (
        "🌍 Set Timezone\n\n"
        "Your bot uses this timezone to display:\n"
        "• Subscription expiry time\n"
        "• Join date\n"
        "• Payment time\n"
        "• Statistics and logs\n"
        "• Scheduler display\n"
        f"{current_line}\n\n"
        "Choose your country below. For a timezone not listed, tap "
        "‘Manual Timezone’ and send its exact IANA name.\n\n"
        "Example: Asia/Kolkata\n"
        "Timezone names are case-sensitive."
    )


def timezone_keyboard(prefix: str, back_callback: str) -> InlineKeyboardMarkup:
    keys = list(TIMEZONE_CHOICES)
    rows = []
    for index in range(0, len(keys), 2):
        row = []
        for key in keys[index:index + 2]:
            label, _ = TIMEZONE_CHOICES[key]
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⌨️ Manual Timezone", callback_data=f"{prefix}manual")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


def timezone_from_key(key: str) -> str | None:
    item = TIMEZONE_CHOICES.get(key)
    return item[1] if item else None


def normalize_timezone(value: str) -> str:
    value = (value or "").strip()
    try:
        ZoneInfo(value)
        return value
    except ZoneInfoNotFoundError:
        lowered = value.casefold()
        for timezone_name in available_timezones():
            if timezone_name.casefold() == lowered:
                return timezone_name
        raise
