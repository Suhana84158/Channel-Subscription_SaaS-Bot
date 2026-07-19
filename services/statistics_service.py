from html import escape

from database.statistics import get_platform_statistics


async def build_platform_statistics_text() -> str:
    stats = await get_platform_statistics()
    revenue = float(stats.get("revenue", 0) or 0)
    generated = stats.get("generated_at")
    generated_text = generated.strftime("%d-%m-%Y %H:%M UTC") if generated else "-"
    return (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{int(stats.get('users', 0))}</b>\n"
        f"🚫 Banned Users: <b>{int(stats.get('banned_users', 0))}</b>\n"
        f"💎 Active Subscriptions: <b>{int(stats.get('active_subscriptions', 0))}</b>\n"
        f"📢 Total Channels: <b>{int(stats.get('channels', 0))}</b>\n\n"
        f"⏳ Pending Payments: <b>{int(stats.get('pending_payments', 0))}</b>\n"
        f"✅ Approved Payments: <b>{int(stats.get('approved_payments', 0))}</b>\n"
        f"❌ Rejected Payments: <b>{int(stats.get('rejected_payments', 0))}</b>\n"
        f"💰 Total Revenue: <b>₹{revenue:,.2f}</b>\n\n"
        f"🕒 Updated: {escape(generated_text)}"
    )
