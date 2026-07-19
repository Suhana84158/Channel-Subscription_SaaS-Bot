"""Formatting helpers for platform statistics."""

from html import escape

from database.statistics import get_platform_statistics


def _money(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"₹{amount:,.2f}"


async def build_platform_statistics_text() -> str:
    stats = await get_platform_statistics()
    generated_at = stats["generated_at"].strftime("%d %b %Y, %H:%M UTC")

    # HTML is used instead of Markdown so names/values cannot accidentally
    # break Telegram formatting if future fields contain special characters.
    return (
        "📊 <b>Bot Statistics</b>\n\n"
        "<b>Users</b>\n"
        f"👥 Total Users: <b>{int(stats['users']):,}</b>\n"
        f"🟢 Active Subscribers: <b>{int(stats['active_users']):,}</b>\n"
        f"🚫 Banned Users: <b>{int(stats['banned_users']):,}</b>\n\n"
        "<b>Subscriptions & Channels</b>\n"
        f"💎 Active Subscriptions: <b>{int(stats['active_subscriptions']):,}</b>\n"
        f"📚 Total Subscriptions: <b>{int(stats['total_subscriptions']):,}</b>\n"
        f"⌛ Inactive/Expired: <b>{int(stats['expired_subscriptions']):,}</b>\n"
        f"📢 Total Channels: <b>{int(stats['channels']):,}</b>\n\n"
        "<b>Payments</b>\n"
        f"⏳ Pending: <b>{int(stats['pending_payments']):,}</b>\n"
        f"⚙️ Processing: <b>{int(stats['processing_payments']):,}</b>\n"
        f"✅ Approved: <b>{int(stats['approved_payments']):,}</b>\n"
        f"❌ Rejected: <b>{int(stats['rejected_payments']):,}</b>\n"
        f"🕒 Expired: <b>{int(stats['expired_payments']):,}</b>\n\n"
        "<b>Revenue</b>\n"
        f"💰 Total: <b>{escape(_money(stats['revenue']))}</b>\n"
        f"📅 Last 24 hours: <b>{escape(_money(stats['revenue_24h']))}</b>\n"
        f"🗓 Last 7 days: <b>{escape(_money(stats['revenue_7d']))}</b>\n"
        f"📆 Last 30 days: <b>{escape(_money(stats['revenue_30d']))}</b>\n\n"
        f"<i>Generated: {escape(generated_at)}</i>"
    )
