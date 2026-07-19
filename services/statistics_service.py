from database.statistics import get_platform_statistics


async def build_platform_statistics_text() -> str:
    stats = await get_platform_statistics()
    revenue = float(stats.get("revenue", 0) or 0)
    return (
        "📊 *Bot Statistics*\n\n"
        f"👥 Total Users: *{stats['users']}*\n"
        f"🚫 Banned Users: *{stats['banned_users']}*\n"
        f"💎 Active Subscriptions: *{stats['active_subscriptions']}*\n"
        f"📢 Total Channels: *{stats['channels']}*\n\n"
        f"⏳ Pending Payments: *{stats['pending_payments']}*\n"
        f"✅ Approved Payments: *{stats['approved_payments']}*\n"
        f"❌ Rejected Payments: *{stats['rejected_payments']}*\n"
        f"💰 Total Revenue: *₹{revenue:,.2f}*"
    )
