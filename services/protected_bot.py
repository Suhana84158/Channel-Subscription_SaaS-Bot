from typing import Any, Optional

from telegram.ext import ExtBot


class ProtectedExtBot(ExtBot):
    """Clone bot that protects newly sent content in private user chats.

    Protection is deliberately not applied to:
    - the clone bot seller/owner
    - groups, supergroups, and channels (negative chat IDs)
    """

    def __init__(self, token: str, owner_id: int, **kwargs: Any):
        super().__init__(token=token, **kwargs)
        self._content_protection_enabled = False
        self._seller_owner_id = int(owner_id)

    @property
    def content_protection_enabled(self) -> bool:
        return bool(self._content_protection_enabled)

    def set_content_protection(self, enabled: bool) -> None:
        self._content_protection_enabled = bool(enabled)

    def _extract_chat_id(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Optional[int]:
        value = kwargs.get("chat_id")
        if value is None and args:
            value = args[0]
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _protected_kwargs(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        data = dict(kwargs)
        chat_id = self._extract_chat_id(args, data)
        if (
            self._content_protection_enabled
            and chat_id is not None
            and chat_id > 0
            and chat_id != self._seller_owner_id
        ):
            data["protect_content"] = True
        return data

    async def send_message(self, *args: Any, **kwargs: Any):
        return await super().send_message(*args, **self._protected_kwargs(args, kwargs))

    async def send_photo(self, *args: Any, **kwargs: Any):
        return await super().send_photo(*args, **self._protected_kwargs(args, kwargs))

    async def send_video(self, *args: Any, **kwargs: Any):
        return await super().send_video(*args, **self._protected_kwargs(args, kwargs))

    async def send_animation(self, *args: Any, **kwargs: Any):
        return await super().send_animation(*args, **self._protected_kwargs(args, kwargs))

    async def send_audio(self, *args: Any, **kwargs: Any):
        return await super().send_audio(*args, **self._protected_kwargs(args, kwargs))

    async def send_document(self, *args: Any, **kwargs: Any):
        return await super().send_document(*args, **self._protected_kwargs(args, kwargs))

    async def send_voice(self, *args: Any, **kwargs: Any):
        return await super().send_voice(*args, **self._protected_kwargs(args, kwargs))

    async def send_video_note(self, *args: Any, **kwargs: Any):
        return await super().send_video_note(*args, **self._protected_kwargs(args, kwargs))

    async def send_sticker(self, *args: Any, **kwargs: Any):
        return await super().send_sticker(*args, **self._protected_kwargs(args, kwargs))

    async def send_media_group(self, *args: Any, **kwargs: Any):
        return await super().send_media_group(*args, **self._protected_kwargs(args, kwargs))

    async def copy_message(self, *args: Any, **kwargs: Any):
        return await super().copy_message(*args, **self._protected_kwargs(args, kwargs))

    async def copy_messages(self, *args: Any, **kwargs: Any):
        return await super().copy_messages(*args, **self._protected_kwargs(args, kwargs))

    async def forward_message(self, *args: Any, **kwargs: Any):
        return await super().forward_message(*args, **self._protected_kwargs(args, kwargs))
