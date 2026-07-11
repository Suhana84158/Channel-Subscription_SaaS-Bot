import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict, InvalidToken, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from database.seller_bots import (
    get_all_active_bots,
    get_bot,
    get_decrypted_bot_token,
    set_runtime_status,
)
from database.seller_data import (
    ensure_seller_defaults,
    get_active_seller_plans,
    get_seller_settings,
)

logger = logging.getLogger(__name__)


@dataclass
class RunningSellerBot:
    owner_id: int
    bot_id: int
    application: Application


class SellerBotManager:
    """Starts and stops seller-owned Telegram bots in the main process."""

    def __init__(self) -> None:
        self._running: Dict[int, RunningSellerBot] = {}
        self._lock = asyncio.Lock()

    def is_running(self, owner_id: int) -> bool:
        return owner_id in self._running

    @staticmethod
    def _main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📋 Plans", callback_data="child_plans"),
                    InlineKeyboardButton("💳 Buy", callback_data="child_buy"),
                ],
                [
                    InlineKeyboardButton("👤 My Profile", callback_data="child_profile"),
                    InlineKeyboardButton("🔄 Renew", callback_data="child_renew"),
                ],
                [
                    InlineKeyboardButton("🎁 Referral", callback_data="child_referral"),
                    InlineKeyboardButton("📞 Support", callback_data="child_support"),
                ],
            ]
        )

    async def _child_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = int(context.application.bot_data["seller_owner_id"])
        record = await get_bot(owner_id)
        fallback_name = record.get("bot_name", "Subscription Bot") if record else "Subscription Bot"
        settings = await ensure_seller_defaults(owner_id, fallback_name)
        welcome = settings.get("welcome_message") or f"👋 Welcome to {fallback_name}!"

        await update.effective_message.reply_text(
            f"{welcome}\n\nChoose an option below.",
            reply_markup=self._main_menu(),
        )

    async def _child_menu_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        await query.answer()
        owner_id = int(context.application.bot_data["seller_owner_id"])
        action = query.data

        if action in {"child_plans", "child_buy", "child_renew"}:
            plans = await get_active_seller_plans(owner_id)
            if not plans:
                text = "📋 No subscription plans have been added yet."
            else:
                settings = await get_seller_settings(owner_id)
                currency = settings.get("currency", "INR")
                lines = ["📋 Available Plans\n"]
                for plan in plans:
                    name = plan.get("name", "Plan")
                    duration = plan.get("duration_text", "-")
                    price = plan.get("price", 0)
                    lines.append(f"• {name} — {duration} — {currency} {price}")
                text = "\n".join(lines)

        elif action == "child_profile":
            text = "👤 Your seller-specific profile will appear here in the next step."
        elif action == "child_referral":
            text = "🎁 Seller-specific referral rewards will be connected after subscriptions."
        elif action == "child_support":
            settings = await get_seller_settings(owner_id)
            support = settings.get("support_username") or "Not set by seller"
            text = f"📞 Support: {support}"
        else:
            text = "This option is being connected."

        await query.edit_message_text(text, reply_markup=self._main_menu())

    def _build_child_application(self, token: str, owner_id: int) -> Application:
        app = Application.builder().token(token).build()
        app.bot_data["seller_owner_id"] = owner_id
        app.add_handler(CommandHandler("start", self._child_start))
        app.add_handler(CallbackQueryHandler(self._child_menu_callback, pattern=r"^child_"))
        return app

    async def start_bot(self, owner_id: int) -> bool:
        async with self._lock:
            if owner_id in self._running:
                return True

            record = await get_bot(owner_id)
            if not record or not record.get("active"):
                return False

            token = await get_decrypted_bot_token(owner_id)
            if not token:
                await set_runtime_status(owner_id, "token_missing", "Encrypted token is missing")
                return False

            app: Optional[Application] = None
            try:
                await ensure_seller_defaults(owner_id, record.get("bot_name", "Subscription Bot"))
                app = self._build_child_application(token, owner_id)
                await app.initialize()
                await app.start()
                if app.updater is None:
                    raise RuntimeError("Updater is unavailable for seller bot")
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )

                self._running[owner_id] = RunningSellerBot(
                    owner_id=owner_id,
                    bot_id=int(record["bot_id"]),
                    application=app,
                )
                await set_runtime_status(owner_id, "running", None)
                logger.info("Seller bot started: owner=%s bot=%s", owner_id, record.get("bot_id"))
                return True

            except (InvalidToken, Conflict, TelegramError, RuntimeError) as exc:
                logger.exception("Could not start seller bot for owner %s", owner_id)
                await set_runtime_status(owner_id, "error", str(exc)[:500])
                if app is not None:
                    await self._safe_shutdown_application(app)
                return False
            except Exception as exc:
                logger.exception("Unexpected seller bot start error for owner %s", owner_id)
                await set_runtime_status(owner_id, "error", str(exc)[:500])
                if app is not None:
                    await self._safe_shutdown_application(app)
                return False

    async def _safe_shutdown_application(self, app: Application) -> None:
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
        except Exception:
            logger.exception("Error stopping seller bot updater")
        try:
            if app.running:
                await app.stop()
        except Exception:
            logger.exception("Error stopping seller bot application")
        try:
            await app.shutdown()
        except Exception:
            logger.exception("Error shutting down seller bot application")

    async def stop_bot(self, owner_id: int, runtime_status: str = "paused") -> bool:
        async with self._lock:
            running = self._running.pop(owner_id, None)
            if not running:
                await set_runtime_status(owner_id, runtime_status, None)
                return True

            await self._safe_shutdown_application(running.application)
            await set_runtime_status(owner_id, runtime_status, None)
            logger.info("Seller bot stopped: owner=%s", owner_id)
            return True

    async def restart_bot(self, owner_id: int) -> bool:
        await self.stop_bot(owner_id, runtime_status="restarting")
        return await self.start_bot(owner_id)

    async def restore_active_bots(self) -> dict:
        records = await get_all_active_bots()
        started = 0
        failed = 0

        for record in records:
            owner_id = int(record["owner_id"])
            if await self.start_bot(owner_id):
                started += 1
            else:
                failed += 1

        logger.info("Seller bot restore complete: started=%s failed=%s", started, failed)
        return {"started": started, "failed": failed}

    async def shutdown_all(self) -> None:
        owner_ids = list(self._running.keys())
        for owner_id in owner_ids:
            await self.stop_bot(owner_id, runtime_status="service_stopped")


bot_manager = SellerBotManager()
