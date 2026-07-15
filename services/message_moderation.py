"""Runtime message moderation for seller/clone bots.

This module reads each seller's deleting-message settings and decides whether an
incoming group/supergroup message should be deleted.  UI and handler
registration are intentionally kept outside this file.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Sequence, Tuple
from urllib.parse import urlparse

from telegram import Message, MessageEntity, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from database.deleting_messages import (
    get_deleting_message_settings,
    increment_deletion_stat,
    save_delete_all_position,
)

logger = logging.getLogger(__name__)

URL_RE = re.compile(
    r"(?ix)\b(?:"
    r"(?:https?://|ftp://|www\.)[^\s<>()]+"
    r"|(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>()]+"
    r"|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|org|net|in|io|co|me|tv|app|site|xyz|info|biz|link|online|shop|dev|ai)"
    r"(?:/[^\s<>()]*)?"
    r")"
)

PLATFORM_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "telegram": ("t.me", "telegram.me", "telegram.dog"),
    "instagram": ("instagram.com", "instagr.am"),
    "youtube": ("youtube.com", "youtu.be"),
    "facebook": ("facebook.com", "fb.com", "fb.me", "m.me"),
    "x_twitter": ("x.com", "twitter.com", "t.co"),
    "tiktok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "discord": ("discord.com", "discord.gg", "discordapp.com"),
}

MEDIA_FIELDS: Dict[str, str] = {
    "photo": "photo",
    "video": "video",
    "animation": "animation",
    "document": "document",
    "audio": "audio",
    "voice": "voice",
    "sticker": "sticker",
    "video_note": "video_note",
}

SERVICE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "join": ("new_chat_members",),
    "exit": ("left_chat_member",),
    "photos": ("new_chat_photo", "delete_chat_photo"),
    "title": ("new_chat_title",),
    "pinned": ("pinned_message",),
    "topic": (
        "forum_topic_created",
        "forum_topic_closed",
        "forum_topic_reopened",
        "forum_topic_edited",
        "general_forum_topic_hidden",
        "general_forum_topic_unhidden",
    ),
    "boost": ("boost_added",),
    "video_chats": (
        "video_chat_scheduled",
        "video_chat_started",
        "video_chat_ended",
        "video_chat_participants_invited",
    ),
    "checklist": ("checklist", "checklist_tasks_done", "checklist_tasks_added"),
    "community": (
        "connected_website",
        "write_access_allowed",
        "users_shared",
        "chat_shared",
        "direct_message_price_changed",
    ),
}


@dataclass(frozen=True)
class ModerationDecision:
    should_delete: bool
    reason: Optional[str] = None
    stat_name: Optional[str] = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _is_forwarded(message: Message) -> bool:
    return bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_date", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_sender_name", None)
    )


def _has_selected_media(message: Message, section: Dict[str, Any]) -> bool:
    for setting_key, message_field in MEDIA_FIELDS.items():
        if section.get(setting_key) and getattr(message, message_field, None):
            return True
    return False


def _is_service_message(message: Message, section: Dict[str, Any]) -> bool:
    for setting_key, fields in SERVICE_FIELDS.items():
        if not section.get(setting_key):
            continue
        for field in fields:
            value = getattr(message, field, None)
            if value:
                return True
    return False


def _extract_urls(message: Message) -> Sequence[str]:
    text = message.text or message.caption or ""
    entities: Iterable[MessageEntity] = message.entities or message.caption_entities or ()
    found = []

    for entity in entities:
        if entity.type == MessageEntity.TEXT_LINK and entity.url:
            found.append(entity.url)
        elif entity.type == MessageEntity.URL:
            try:
                found.append(text[entity.offset : entity.offset + entity.length])
            except Exception:
                pass

    found.extend(match.group(0) for match in URL_RE.finditer(text))

    unique = []
    seen = set()
    for item in found:
        cleaned = str(item or "").strip().rstrip(".,!?;:)]}>'\"")
        if cleaned and cleaned.lower() not in seen:
            unique.append(cleaned)
            seen.add(cleaned.lower())
    return unique


def _hostname(value: str) -> str:
    candidate = value.strip().lower()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", candidate):
        candidate = "https://" + candidate
    try:
        host = (urlparse(candidate).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    return host


def _domain_matches(host: str, domain: str) -> bool:
    domain = domain.lower().removeprefix("www.").strip(".")
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _contains_blocked_link(message: Message, section: Dict[str, Any]) -> bool:
    if not section.get("enabled"):
        return False

    urls = _extract_urls(message)
    if not urls:
        return False

    if section.get("all_links", True):
        return True

    custom_domains = tuple(str(x).lower() for x in section.get("custom_domains", []) if x)

    for url in urls:
        host = _hostname(url)
        if not host:
            continue

        if any(_domain_matches(host, domain) for domain in custom_domains):
            return True

        for setting_key, domains in PLATFORM_DOMAINS.items():
            if section.get(setting_key) and any(_domain_matches(host, d) for d in domains):
                return True

    return False


def _looks_like_command(message: Message, prefixes: Sequence[str]) -> bool:
    text = _message_text(message)
    if not text:
        return False
    return any(prefix and text.startswith(prefix) for prefix in prefixes)


async def _is_chat_admin(context: ContextTypes.DEFAULT_TYPE, message: Message) -> bool:
    user = message.from_user
    chat = message.chat
    if not user or not chat:
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {"administrator", "creator", "owner"}
    except TelegramError:
        logger.debug("Could not check admin status chat=%s user=%s", chat.id, user.id)
        return False


class MessageModerationService:
    """Evaluate and delete messages according to one seller's settings."""

    def __init__(self, owner_id: int):
        self.owner_id = int(owner_id)

    async def evaluate(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        settings: Optional[Dict[str, Any]] = None,
    ) -> ModerationDecision:
        message = update.effective_message
        if not message or message.chat.type not in {"group", "supergroup"}:
            return ModerationDecision(False)

        if message.from_user and message.from_user.is_bot:
            return ModerationDecision(False)

        settings = settings or await get_deleting_message_settings(self.owner_id)
        if not settings.get("enabled", True):
            return ModerationDecision(False)

        user_id = message.from_user.id if message.from_user else None
        if user_id is not None:
            if settings.get("ignore_owner", True) and user_id == self.owner_id:
                return ModerationDecision(False)
            if user_id in {int(x) for x in settings.get("whitelisted_user_ids", [])}:
                return ModerationDecision(False)

        admin_status: Optional[bool] = None

        async def is_admin() -> bool:
            nonlocal admin_status
            if admin_status is None:
                admin_status = await _is_chat_admin(context, message)
            return admin_status

        # Service messages often have no normal sender and must be checked first.
        service_settings = settings.get("service_messages", {})
        if _is_service_message(message, service_settings):
            if settings.get("ignore_admins", True) and message.from_user and await is_admin():
                return ModerationDecision(False)
            return ModerationDecision(True, "service_message", "service_messages_deleted")

        if user_id is not None and settings.get("ignore_admins", True) and await is_admin():
            return ModerationDecision(False)

        command_settings = settings.get("delete_commands", {})
        prefixes = command_settings.get("prefixes") or ["/"]
        if _looks_like_command(message, prefixes):
            sender_is_admin = await is_admin()
            delete_command = (
                command_settings.get("admins", False)
                if sender_is_admin
                else command_settings.get("users", False)
            )
            if delete_command:
                return ModerationDecision(True, "command", "commands_deleted")

        if _contains_blocked_link(message, settings.get("link_protection", {})):
            return ModerationDecision(True, "link", "links_deleted")

        forwarded_settings = settings.get("forwarded_media", {})
        if (
            forwarded_settings.get("enabled")
            and _is_forwarded(message)
            and _has_selected_media(message, forwarded_settings)
        ):
            return ModerationDecision(True, "forwarded_media", "forwarded_media_deleted")

        return ModerationDecision(False)

    async def moderate(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        """Delete a matching message. Returns True only when deletion succeeds."""
        message = update.effective_message
        if not message:
            return False

        decision = await self.evaluate(update, context)
        if not decision.should_delete:
            return False

        try:
            await message.delete()
        except (BadRequest, Forbidden, TelegramError) as exc:
            logger.warning(
                "Moderation deletion failed owner=%s chat=%s message=%s reason=%s: %s",
                self.owner_id,
                message.chat_id,
                message.message_id,
                decision.reason,
                exc,
            )
            try:
                await increment_deletion_stat(self.owner_id, "failed_deletions")
            except Exception:
                logger.exception("Failed to record moderation failure statistic")
            return False

        if decision.stat_name:
            try:
                await increment_deletion_stat(self.owner_id, decision.stat_name)
            except Exception:
                logger.exception("Failed to record moderation statistic")

        return True


async def moderate_seller_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: Optional[int] = None,
) -> bool:
    """Convenience entry point for clone-bot MessageHandler callbacks."""
    resolved_owner = owner_id
    if resolved_owner is None:
        resolved_owner = context.application.bot_data.get("seller_owner_id")
    if resolved_owner is None:
        logger.error("seller_owner_id is missing; moderation skipped")
        return False

    return await MessageModerationService(int(resolved_owner)).moderate(update, context)


async def delete_message_range(
    *,
    bot: Any,
    owner_id: int,
    chat_id: int,
    start_message_id: int,
    end_message_id: int,
    progress_callback: Optional[Callable[[int, int, int], Any]] = None,
    checkpoint_every: int = 25,
) -> Dict[str, int]:
    """Best-effort bulk deletion used by the future Delete All Messages UI.

    Telegram may refuse old messages or messages the bot cannot delete. This
    helper continues after failures and stores a resumable checkpoint.
    """
    owner_id = int(owner_id)
    chat_id = int(chat_id)
    start = max(1, int(start_message_id))
    end = max(start, int(end_message_id))
    checkpoint_every = max(1, int(checkpoint_every))

    deleted = 0
    failed = 0
    processed = 0

    for message_id in range(end, start - 1, -1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except TelegramError:
            failed += 1

        processed += 1

        if processed % checkpoint_every == 0:
            await save_delete_all_position(owner_id, chat_id, message_id)
            if progress_callback:
                await _maybe_await(progress_callback(processed, deleted, failed))

    await save_delete_all_position(owner_id, chat_id, start)

    if deleted:
        await increment_deletion_stat(owner_id, "delete_all_deleted", deleted)
    if failed:
        await increment_deletion_stat(owner_id, "failed_deletions", failed)

    if progress_callback:
        await _maybe_await(progress_callback(processed, deleted, failed))

    return {"processed": processed, "deleted": deleted, "failed": failed}
