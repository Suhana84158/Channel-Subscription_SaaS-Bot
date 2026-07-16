import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database.seller_data import get_channels, get_subscription
from database.subscription_guard import (
    add_whitelist,
    is_whitelisted,
    log_guard_event,
    mark_invite_used,
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


async def subscription_guard_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = update.chat_member
    if not event:
        return

    owner_id = int(context.application.bot_data.get("seller_owner_id") or 0)
    if not owner_id or not await _connected_chat(owner_id, event.chat.id):
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
    if actor and actor.id != user.id and await _is_admin(context.bot, event.chat.id, actor.id):
        await add_whitelist(owner_id, event.chat.id, user.id, actor.id)
        await log_guard_event(owner_id, event.chat.id, user.id, "whitelisted", "Added by chat admin/owner")
        return

    if await _is_admin(context.bot, event.chat.id, user.id):
        return
    if await is_whitelisted(owner_id, event.chat.id, user.id):
        return

    invite = getattr(event, "invite_link", None)
    if invite and getattr(invite, "invite_link", None):
        try:
            await context.bot.revoke_chat_invite_link(event.chat.id, invite.invite_link)
        except TelegramError:
            pass
        await mark_invite_used(owner_id, invite.invite_link)

    if await _active(owner_id, user.id):
        await log_guard_event(owner_id, event.chat.id, user.id, "allowed", "Active subscription")
        return

    try:
        await _remove_member(context.bot, event.chat.id, user.id)
        await log_guard_event(owner_id, event.chat.id, user.id, "removed", "No active subscription")
        try:
            await context.bot.send_message(
                user.id,
                "🚫 You were removed from the premium group/channel because no active subscription was found.\n\nPlease buy or renew a plan, then use the fresh invite link sent by this bot.",
            )
        except TelegramError:
            pass
    except TelegramError as exc:
        logger.warning("Subscription guard remove failed owner=%s chat=%s user=%s: %s", owner_id, event.chat.id, user.id, exc)
        await log_guard_event(owner_id, event.chat.id, user.id, "remove_failed", str(exc))


async def subscription_guard_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if not message or not chat or not message.new_chat_members:
        return

    owner_id = int(context.application.bot_data.get("seller_owner_id") or 0)
    if not owner_id or not await _connected_chat(owner_id, chat.id):
        return

    actor_is_admin = bool(actor and await _is_admin(context.bot, chat.id, actor.id))
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        if actor_is_admin and actor.id != user.id:
            await add_whitelist(owner_id, chat.id, user.id, actor.id)
            continue
        if await _is_admin(context.bot, chat.id, user.id) or await is_whitelisted(owner_id, chat.id, user.id):
            continue
        if await _active(owner_id, user.id):
            continue
        try:
            await _remove_member(context.bot, chat.id, user.id)
            await log_guard_event(owner_id, chat.id, user.id, "removed", "No active subscription")
        except TelegramError as exc:
            logger.warning("Subscription guard fallback failed owner=%s chat=%s user=%s: %s", owner_id, chat.id, user.id, exc)
