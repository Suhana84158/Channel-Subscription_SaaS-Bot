from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.seller_data import get_channels, get_seller_settings
from database.subscription_guard import (
    clear_guard_logs,
    get_guard_settings,
    guard_statistics,
    recent_guard_logs,
    reset_guard_settings,
    set_guard_setting,
)
from services.subscription_guard import force_sync_known_users
from utils.timezone import format_local_datetime

LABELS = {
    "enabled": "Master Guard",
    "unauthorized_join_protection": "Unauthorized Join Protection",
    "auto_remove_expired": "Auto Remove Expired Users",
    "auto_revoke_invites": "Auto Revoke Invite Links",
    "whitelist_admin_added": "Admin/Owner Whitelist",
    "log_events": "Log All Events",
    "notify_seller": "Notify Seller",
}


def _toggle(label: str, key: str, value: bool):
    return InlineKeyboardButton(
        f"{'✅' if value else '❌'} {label}",
        callback_data=f"sg_toggle:{key}",
    )


def guard_menu(settings):
    return InlineKeyboardMarkup([
        [_toggle("Subscription Guard", "enabled", settings["enabled"])],
        [InlineKeyboardButton("🧰 Subscription Enforcement", callback_data="sg_enforcement")],
        [InlineKeyboardButton("🔄 Force Sync", callback_data="sg_sync_confirm")],
        [InlineKeyboardButton("📋 Guard Logs", callback_data="sg_logs"), InlineKeyboardButton("📊 Statistics", callback_data="sg_stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="sg_settings")],
        [InlineKeyboardButton("🧹 Clear Logs", callback_data="sg_clear_confirm")],
        [InlineKeyboardButton("⬅ Admin Panel", callback_data="a_home")],
    ])


def settings_menu(settings):
    rows = [[_toggle(LABELS[key], key, settings[key])] for key in (
        "unauthorized_join_protection", "auto_remove_expired", "auto_revoke_invites",
        "whitelist_admin_added", "log_events", "notify_seller",
    )]
    rows += [
        [InlineKeyboardButton("♻️ Reset Settings", callback_data="sg_reset_confirm")],
        [InlineKeyboardButton("⬅ Subscription Guard", callback_data="sg_home")],
    ]
    return InlineKeyboardMarkup(rows)


def home_text(settings):
    return (
        "🛡 <b>Subscription Guard</b>\n\n"
        f"Status: {'🟢 Enabled' if settings['enabled'] else '🔴 Disabled'}\n\n"
        "Subscription Enforcement is included inside this page. It protects "
        "connected groups by checking new joins and removing users whose access "
        "is expired, inactive or banned.\n\n"
        "• Active subscribers are allowed\n"
        "• Admins/owner/whitelist are skipped\n"
        "• Used personal invite links can be revoked\n"
        "• Repeated unauthorized attempts are counted"
    )


async def _edit(query, text, markup):
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def subscription_guard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = int(context.application.bot_data.get("seller_owner_id") or 0)
    if not query or not owner_id:
        return
    if query.from_user.id != owner_id:
        await query.answer("Only the clone bot seller/admin can use this panel.", show_alert=True)
        return
    await query.answer()
    action = query.data or ""
    settings = await get_guard_settings(owner_id)

    if action == "sg_home":
        return await _edit(query, home_text(settings), guard_menu(settings))
    if action.startswith("sg_toggle:"):
        key = action.split(":", 1)[1]
        settings = await set_guard_setting(owner_id, key, not bool(settings.get(key)))
        if key == "enabled":
            return await _edit(query, home_text(settings), guard_menu(settings))
        return await _edit(query, "⚙️ <b>Subscription Guard Settings</b>\n\nChoose which protections should be active.", settings_menu(settings))
    if action == "sg_settings":
        return await _edit(query, "⚙️ <b>Subscription Guard Settings</b>\n\nChoose which protections should be active.", settings_menu(settings))
    if action == "sg_enforcement":
        channels = await get_channels(owner_id)
        text = (
            "🧰 <b>Subscription Enforcement</b>\n\n"
            f"Connected chats: <b>{len(channels)}</b>\n\n"
            "Automatic enforcement:\n"
            "• Unauthorized join → remove\n"
            "• Expired/inactive subscription → remove\n"
            "• Banned user → remove\n"
            "• Issued invite links → revoke\n"
            "• Admin/owner/whitelist → skip\n\n"
            "New joins are checked in real time. Force Sync checks every user already known to this clone bot."
        )
        return await _edit(query, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Run Force Sync", callback_data="sg_sync_confirm")],
            [InlineKeyboardButton("⬅ Subscription Guard", callback_data="sg_home")],
        ]))
    if action == "sg_sync_confirm":
        return await _edit(query,
            "🔄 <b>Run Force Sync?</b>\n\nThis checks all users recorded by this clone bot, removes expired/banned users from connected chats and revokes their active invite links.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Start Sync", callback_data="sg_sync")],
                [InlineKeyboardButton("❌ Cancel", callback_data="sg_home")],
            ]),
        )
    if action == "sg_sync":
        await _edit(query, "⏳ <b>Force Sync Running...</b>\n\nPlease wait.", None)
        report = await force_sync_known_users(context.bot, owner_id)
        text = (
            "✅ <b>Force Sync Completed</b>\n\n"
            f"Users Checked: {report['users_checked']}\n"
            f"Expired/Inactive: {report['expired_or_inactive']}\n"
            f"Banned Users: {report['banned']}\n"
            f"Chat Removals: {report['removed']}\n"
            f"Remove Failed: {report['remove_failed']}\n"
            f"Invite Links Revoked: {report['invites_revoked']}\n\n"
            "Note: Telegram bots cannot request a complete member list. Unknown users are still checked automatically whenever they join."
        )
        return await _edit(query, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Run Again", callback_data="sg_sync_confirm")],
            [InlineKeyboardButton("⬅ Subscription Guard", callback_data="sg_home")],
        ]))
    if action == "sg_logs":
        logs = await recent_guard_logs(owner_id, 12)
        seller_settings = await get_seller_settings(owner_id)
        timezone_name = seller_settings.get("timezone") or "Asia/Kolkata"
        if not logs:
            text = "📋 <b>Guard Logs</b>\n\nNo guard events recorded yet."
        else:
            icons = {"allowed":"🟢", "removed":"🔴", "remove_failed":"⚠️", "whitelisted":"🟡", "admin_skipped":"🟡", "invite_revoked":"🔗"}
            lines = ["📋 <b>Guard Logs</b>", f"Timezone: <b>{timezone_name}</b>", ""]
            for row in logs:
                when = format_local_datetime(row.get("created_at"), timezone_name, "%d-%m %I:%M %p")
                action_name = str(row.get("action", "event"))
                lines.append(f"{icons.get(action_name,'•')} <b>{action_name.replace('_',' ').title()}</b>")
                lines.append(f"User: <code>{row.get('user_id','-')}</code> | Chat: <code>{row.get('chat_id','-')}</code>")
                lines.append(f"Reason: {row.get('reason') or '-'}")
                if row.get("attempts"):
                    lines.append(f"Attempts: {row['attempts']}")
                lines.append(when + "\n")
            text = "\n".join(lines)
        return await _edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="sg_logs")],[InlineKeyboardButton("⬅ Subscription Guard", callback_data="sg_home")]]))
    if action == "sg_stats":
        stats = await guard_statistics(owner_id)
        text = (
            "📊 <b>Subscription Guard Statistics</b>\n\n"
            f"✅ Allowed Joins: {stats.get('allowed',0)}\n"
            f"❌ Unauthorized Removed: {stats.get('removed',0)}\n"
            f"⚠️ Remove Failed: {stats.get('remove_failed',0)}\n"
            f"👮 Admin/Whitelist Skipped: {stats.get('admin_skipped',0)+stats.get('whitelisted',0)}\n"
            f"🔗 Invite Links Revoked: {stats.get('invite_revoked',0)}\n"
            f"🚨 Total Unauthorized Attempts: {stats.get('join_attempts',0)}"
        )
        return await _edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="sg_stats")],[InlineKeyboardButton("⬅ Subscription Guard", callback_data="sg_home")]]))
    if action == "sg_clear_confirm":
        return await _edit(query, "🧹 <b>Clear Guard Logs?</b>\n\nThis clears logs and join-attempt counters for this clone bot.", InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, Clear", callback_data="sg_clear")],[InlineKeyboardButton("❌ Cancel", callback_data="sg_home")]]))
    if action == "sg_clear":
        await clear_guard_logs(owner_id)
        return await _edit(query, home_text(settings), guard_menu(settings))
    if action == "sg_reset_confirm":
        return await _edit(query, "♻️ <b>Reset Subscription Guard settings?</b>", InlineKeyboardMarkup([[InlineKeyboardButton("✅ Reset", callback_data="sg_reset")],[InlineKeyboardButton("❌ Cancel", callback_data="sg_settings")]]))
    if action == "sg_reset":
        settings = await reset_guard_settings(owner_id)
        return await _edit(query, "⚙️ <b>Subscription Guard Settings</b>\n\nSettings reset to defaults.", settings_menu(settings))


def subscription_guard_handlers():
    return [CallbackQueryHandler(
        subscription_guard_callback,
        pattern=r"^sg_(home|enforcement|sync|sync_confirm|logs|stats|settings|clear|clear_confirm|reset|reset_confirm|toggle:.+)$",
    )]
