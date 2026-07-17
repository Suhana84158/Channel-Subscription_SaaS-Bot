import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database.mongo import get_database
from database.seller_data import get_channels, get_subscription
from database.subscription_guard import (
    add_whitelist, get_guard_settings, is_whitelisted, log_guard_event,
    active_invites_for_user, deactivate_invite, mark_invite_used, record_join_attempt,
)

logger = logging.getLogger(__name__)


def _aware(value):
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _connected_chat(owner_id: int, chat_id: int) -> bool:
    channels = await get_channels(int(owner_id))
    return any(int(item.get("chat_id")) == int(chat_id) for item in channels)


async def _active(owner_id: int, user_id: int) -> bool:
    sub = await get_subscription(int(owner_id), int(user_id))
    if not sub or not sub.get("active"):
        return False
    expiry = _aware(sub.get("expiry_date"))
    return bool(expiry and expiry > datetime.now(timezone.utc))


async def _is_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return getattr(member, "status", "") in {"creator", "administrator"}
    except TelegramError:
        return False


async def _remove_member(bot, chat_id: int, user_id: int):
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)


async def _alert_seller(context: ContextTypes.DEFAULT_TYPE, owner_id: int, event, user, attempts: int):
    settings = await get_guard_settings(owner_id)
    if not settings.get("notify_seller", True):
        return
    username = f"@{user.username}" if user.username else user.full_name
    try:
        await context.bot.send_message(
            owner_id,
            "🚨 <b>Subscription Guard Alert</b>\n\n"
            f"Unauthorized user removed.\n\n👤 User: {username}\n"
            f"🆔 User ID: <code>{user.id}</code>\n📢 Chat: {event.chat.title or event.chat.id}\n"
            f"🔁 Attempts: {attempts}\n❌ Reason: No active subscription",
            parse_mode="HTML",
        )
    except TelegramError:
        pass


async def subscription_guard_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = update.chat_member
    if not event:
        return
    owner_id = int(context.application.bot_data.get("seller_owner_id") or 0)
    if not owner_id or not await _connected_chat(owner_id, event.chat.id):
        return
    settings = await get_guard_settings(owner_id)
    if not settings.get("enabled", True):
        return

    old_status = getattr(event.old_chat_member, "status", "")
    new_status = getattr(event.new_chat_member, "status", "")
    joined = old_status in {"left", "kicked"} and new_status in {"member", "restricted", "administrator", "creator"}
    if not joined:
        return

    user = event.new_chat_member.user
    if user.is_bot:
        return
    actor = event.from_user
    if settings.get("whitelist_admin_added", True) and actor and actor.id != user.id and await _is_admin(context.bot, event.chat.id, actor.id):
        await add_whitelist(owner_id, event.chat.id, user.id, actor.id)
        await log_guard_event(owner_id, event.chat.id, user.id, "whitelisted", "Added by chat admin/owner")
        return
    if await _is_admin(context.bot, event.chat.id, user.id) or await is_whitelisted(owner_id, event.chat.id, user.id):
        await log_guard_event(owner_id, event.chat.id, user.id, "admin_skipped", "Admin/owner/whitelist")
        return

    invite = getattr(event, "invite_link", None)
    if settings.get("auto_revoke_invites", True) and invite and getattr(invite, "invite_link", None):
        try:
            await context.bot.revoke_chat_invite_link(event.chat.id, invite.invite_link)
            await log_guard_event(owner_id, event.chat.id, user.id, "invite_revoked", "Used invite link revoked")
        except TelegramError:
            pass
        await mark_invite_used(owner_id, invite.invite_link)

    if await _active(owner_id, user.id):
        await log_guard_event(owner_id, event.chat.id, user.id, "allowed", "Active subscription")
        return
    if not settings.get("unauthorized_join_protection", True):
        await log_guard_event(owner_id, event.chat.id, user.id, "allowed_unprotected", "Protection disabled")
        return

    attempts = await record_join_attempt(owner_id, event.chat.id, user.id)
    try:
        await _remove_member(context.bot, event.chat.id, user.id)
        await log_guard_event(owner_id, event.chat.id, user.id, "removed", "No active subscription", attempts=attempts)
        await _alert_seller(context, owner_id, event, user, attempts)
        try:
            await context.bot.send_message(
                user.id,
                "🚫 You were removed because no active subscription was found.\n\nPlease buy or renew a plan, then use a fresh invite link sent by this bot.",
            )
        except TelegramError:
            pass
    except TelegramError as exc:
        logger.warning("Subscription guard remove failed owner=%s chat=%s user=%s: %s", owner_id, event.chat.id, user.id, exc)
        await log_guard_event(owner_id, event.chat.id, user.id, "remove_failed", str(exc), attempts=attempts)


async def subscription_guard_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, chat, actor = update.effective_message, update.effective_chat, update.effective_user
    if not message or not chat or not message.new_chat_members:
        return
    owner_id = int(context.application.bot_data.get("seller_owner_id") or 0)
    if not owner_id or not await _connected_chat(owner_id, chat.id):
        return
    settings = await get_guard_settings(owner_id)
    if not settings.get("enabled", True):
        return
    actor_is_admin = bool(actor and await _is_admin(context.bot, chat.id, actor.id))
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        if settings.get("whitelist_admin_added", True) and actor_is_admin and actor.id != user.id:
            await add_whitelist(owner_id, chat.id, user.id, actor.id)
            await log_guard_event(owner_id, chat.id, user.id, "whitelisted", "Added by chat admin/owner")
            continue
        if await _is_admin(context.bot, chat.id, user.id) or await is_whitelisted(owner_id, chat.id, user.id):
            continue
        if await _active(owner_id, user.id):
            await log_guard_event(owner_id, chat.id, user.id, "allowed", "Active subscription")
            continue
        if not settings.get("unauthorized_join_protection", True):
            continue
        attempts = await record_join_attempt(owner_id, chat.id, user.id)
        try:
            await _remove_member(context.bot, chat.id, user.id)
            await log_guard_event(owner_id, chat.id, user.id, "removed", "No active subscription", attempts=attempts)
        except TelegramError as exc:
            logger.warning("Subscription guard fallback failed owner=%s chat=%s user=%s: %s", owner_id, chat.id, user.id, exc)


async def revoke_user_invites(bot, owner_id: int, user_id: int) -> int:
    """Revoke all active invite links created for one subscriber."""
    revoked = 0
    for invite in await active_invites_for_user(owner_id, user_id):
        try:
            await bot.revoke_chat_invite_link(
                int(invite["chat_id"]),
                str(invite["invite_link"]),
            )
            revoked += 1
        except TelegramError:
            pass
        finally:
            await deactivate_invite(owner_id, str(invite["invite_link"]))
    return revoked


async def enforce_user_access(bot, owner_id: int, user_id: int, reason: str) -> dict:
    """Remove a user from every connected chat and revoke issued links."""
    report = {"removed": 0, "remove_failed": 0, "invites_revoked": 0}
    report["invites_revoked"] = await revoke_user_invites(bot, owner_id, user_id)
    for channel in await get_channels(owner_id):
        chat_id = int(channel["chat_id"])
        if await _is_admin(bot, chat_id, user_id) or await is_whitelisted(owner_id, chat_id, user_id):
            await log_guard_event(owner_id, chat_id, user_id, "admin_skipped", reason)
            continue
        try:
            await _remove_member(bot, chat_id, user_id)
            report["removed"] += 1
            await log_guard_event(owner_id, chat_id, user_id, "removed", reason)
        except TelegramError as exc:
            report["remove_failed"] += 1
            await log_guard_event(owner_id, chat_id, user_id, "remove_failed", str(exc))
    return report


async def force_sync_known_users(bot, owner_id: int) -> dict:
    """Synchronize users known to this clone bot.

    Telegram's Bot API cannot enumerate every group member. This safely checks
    all users recorded by the bot and enforces access for expired/banned users.
    New unknown joins remain protected by ChatMember updates.
    """
    db = get_database()
    now = datetime.now(timezone.utc)
    users = await db["seller_users"].find({"owner_id": int(owner_id)}).to_list(length=100000)
    report = {
        "users_checked": 0,
        "expired_or_inactive": 0,
        "banned": 0,
        "removed": 0,
        "remove_failed": 0,
        "invites_revoked": 0,
    }
    for user in users:
        user_id = int(user["user_id"])
        report["users_checked"] += 1
        sub = await get_subscription(owner_id, user_id)
        expiry = _aware((sub or {}).get("expiry_date"))
        banned = bool(user.get("banned"))
        inactive = not (sub and sub.get("active") and expiry and expiry > now)
        if not banned and not inactive:
            continue
        reason = "Banned user" if banned else "Expired or inactive subscription"
        report["banned" if banned else "expired_or_inactive"] += 1
        result = await enforce_user_access(bot, owner_id, user_id, reason)
        for key in ("removed", "remove_failed", "invites_revoked"):
            report[key] += result[key]
    return report
