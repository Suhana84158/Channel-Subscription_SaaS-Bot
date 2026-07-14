"""Helpers for connecting Telegram groups/channels to a seller clone bot."""

from __future__ import annotations

from dataclasses import dataclass

from telegram import Chat
from telegram.error import BadRequest
from telegram.ext import ContextTypes


@dataclass(slots=True)
class ConnectedChat:
    chat_id: int
    title: str
    chat_type: str
    invite_link: str


async def connect_current_group(
    context: ContextTypes.DEFAULT_TYPE,
    chat: Chat,
) -> ConnectedChat:
    """Validate bot permissions and prove that a fresh invite can be created."""
    if chat.type not in {"group", "supergroup"}:
        raise BadRequest("Run /connectgroup inside the target group")

    me = await context.bot.get_me()
    member = await context.bot.get_chat_member(chat.id, me.id)
    status = getattr(member, "status", "")
    can_invite = bool(getattr(member, "can_invite_users", False))

    if status not in {"administrator", "creator"}:
        raise BadRequest("Bot is not an administrator in this group")
    if status != "creator" and not can_invite:
        raise BadRequest("Invite Users permission is disabled")

    invite = await context.bot.create_chat_invite_link(
        chat_id=chat.id,
        member_limit=1,
        name="Connection test",
    )

    return ConnectedChat(
        chat_id=chat.id,
        title=chat.title or "Premium Group",
        chat_type=chat.type,
        invite_link=invite.invite_link,
    )


async def validate_forwarded_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat: Chat,
) -> ConnectedChat:
    """Validate a forwarded channel/group and confirm invite-link permission."""
    if chat.type not in {"channel", "group", "supergroup"}:
        raise BadRequest("Forward a message from a channel or group")

    me = await context.bot.get_me()
    member = await context.bot.get_chat_member(chat.id, me.id)
    status = getattr(member, "status", "")
    can_invite = bool(getattr(member, "can_invite_users", False))

    if status not in {"administrator", "creator"}:
        raise BadRequest("Bot is not an administrator in this chat")
    if status != "creator" and not can_invite:
        raise BadRequest("Invite Users permission is disabled")

    invite = await context.bot.create_chat_invite_link(
        chat_id=chat.id,
        member_limit=1,
        name="Connection test",
    )

    return ConnectedChat(
        chat_id=chat.id,
        title=chat.title or "Premium Chat",
        chat_type=chat.type,
        invite_link=invite.invite_link,
    )
