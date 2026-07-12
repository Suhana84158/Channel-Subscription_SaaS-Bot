import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict, InvalidToken, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database.seller_bots import get_all_active_bots, get_bot, get_decrypted_bot_token, set_runtime_status
from database.seller_data import (
    create_seller_plan,
    delete_seller_plan,
    ensure_seller_defaults,
    get_active_seller_plans,
    get_all_seller_plans,
    get_seller_plan,
    get_seller_settings,
    set_seller_plan_active,
    update_seller_plan,
)

logger = logging.getLogger(__name__)


@dataclass
class RunningSellerBot:
    owner_id: int
    bot_id: int
    application: Application


class SellerBotManager:
    def __init__(self) -> None:
        self._running: Dict[int, RunningSellerBot] = {}
        self._lock = asyncio.Lock()

    def is_running(self, owner_id: int) -> bool:
        return owner_id in self._running

    @staticmethod
    def _main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Plans", callback_data="child_plans"), InlineKeyboardButton("💳 Buy", callback_data="child_buy")],
            [InlineKeyboardButton("👤 My Profile", callback_data="child_profile"), InlineKeyboardButton("🔄 Renew", callback_data="child_renew")],
            [InlineKeyboardButton("🎁 Referral", callback_data="child_referral"), InlineKeyboardButton("📞 Support", callback_data="child_support")],
        ])

    @staticmethod
    def _admin_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Manage Plans", callback_data="seller_admin_plans")],
            [InlineKeyboardButton("📢 Channels / Groups", callback_data="seller_admin_channels")],
            [InlineKeyboardButton("💳 Payment Settings", callback_data="seller_admin_payment")],
            [InlineKeyboardButton("⚙️ Bot Settings", callback_data="seller_admin_settings")],
            [InlineKeyboardButton("📊 Statistics", callback_data="seller_admin_stats")],
        ])

    @staticmethod
    def _plans_admin_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Plan", callback_data="seller_plan_add")],
            [InlineKeyboardButton("📋 View Plans", callback_data="seller_plan_list")],
            [InlineKeyboardButton("⬅ Back", callback_data="seller_admin_home")],
        ])

    @staticmethod
    def _parse_duration(duration_text: str) -> int:
        value = duration_text.strip().lower()
        if len(value) < 2:
            raise ValueError("Invalid duration")
        number = int(value[:-1])
        unit = value[-1]
        if number <= 0:
            raise ValueError("Duration must be greater than zero")
        if unit == "m":
            return number
        if unit == "h":
            return number * 60
        if unit == "d":
            return number * 1440
        raise ValueError("Use m, h or d")

    @classmethod
    def _parse_plan_input(cls, text: str) -> tuple[str, str, int, float]:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) != 3:
            raise ValueError("Use format: Plan Name | Duration | Price")
        name, duration_text, price_text = parts
        if not name:
            raise ValueError("Plan name is required")
        duration_minutes = cls._parse_duration(duration_text)
        price = float(price_text)
        if price < 0:
            raise ValueError("Price cannot be negative")
        return name, duration_text.lower(), duration_minutes, price

    async def _child_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = int(context.application.bot_data["seller_owner_id"])
        record = await get_bot(owner_id)
        fallback_name = record.get("bot_name", "Subscription Bot") if record else "Subscription Bot"
        settings = await ensure_seller_defaults(owner_id, fallback_name)
        welcome = settings.get("welcome_message") or f"👋 Welcome to {fallback_name}!"
        await update.effective_message.reply_text(f"{welcome}\n\nChoose an option below.", reply_markup=self._main_menu())

    async def _child_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = int(context.application.bot_data["seller_owner_id"])
        if update.effective_user.id != owner_id:
            await update.effective_message.reply_text("❌ You are not authorized.")
            return
        context.user_data.clear()
        await update.effective_message.reply_text("🛠 Seller Admin Panel\n\nChoose an option:", reply_markup=self._admin_menu())

    async def _child_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                    lines.append(f"• {plan.get('name', 'Plan')} — {plan.get('duration_text', '-')} — {currency} {plan.get('price', 0):g}")
                text = "\n".join(lines)
        elif action == "child_profile":
            text = "👤 Your seller-specific profile will appear here after subscriptions are connected."
        elif action == "child_referral":
            text = "🎁 Seller-specific referral rewards will be connected after subscriptions."
        elif action == "child_support":
            settings = await get_seller_settings(owner_id)
            text = f"📞 Support: {settings.get('support_username') or 'Not set by seller'}"
        else:
            text = "This option is being connected."
        await query.edit_message_text(text, reply_markup=self._main_menu())

    async def _seller_admin_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        owner_id = int(context.application.bot_data["seller_owner_id"])
        if query.from_user.id != owner_id:
            await query.edit_message_text("❌ You are not authorized.")
            return
        action = query.data

        if action == "seller_admin_home":
            context.user_data.clear()
            await query.edit_message_text("🛠 Seller Admin Panel\n\nChoose an option:", reply_markup=self._admin_menu())
            return
        if action == "seller_admin_plans":
            context.user_data.clear()
            await query.edit_message_text("📦 Plan Management", reply_markup=self._plans_admin_menu())
            return
        if action == "seller_plan_add":
            context.user_data.clear()
            context.user_data["seller_waiting_plan_add"] = True
            await query.edit_message_text(
                "➕ Add Plan\n\nSend in this format:\nPlan Name | Duration | Price\n\nExample:\nPremium | 30d | 199\n\nDuration units:\nm = minutes, h = hours, d = days",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="seller_admin_plans")]]),
            )
            return
        if action == "seller_plan_list":
            plans = await get_all_seller_plans(owner_id)
            if not plans:
                await query.edit_message_text("📋 No plans added yet.", reply_markup=self._plans_admin_menu())
                return
            lines = ["📋 Your Plans\n"]
            keyboard = []
            for plan in plans:
                status = "✅" if plan.get("active") else "⏸"
                plan_id = plan["plan_id"]
                name = plan.get("name", "Plan")
                lines.append(f"{status} {name} — {plan.get('duration_text', '-')} — ₹{plan.get('price', 0):g}")
                keyboard.append([
                    InlineKeyboardButton(f"✏️ Edit {name[:18]}", callback_data=f"seller_plan_edit_{plan_id}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"seller_plan_delete_{plan_id}"),
                ])
                keyboard.append([InlineKeyboardButton("⏸ Disable" if plan.get("active") else "▶️ Enable", callback_data=f"seller_plan_toggle_{plan_id}")])
            keyboard.append([InlineKeyboardButton("⬅ Back", callback_data="seller_admin_plans")])
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
            return
        if action.startswith("seller_plan_edit_"):
            plan_id = action.replace("seller_plan_edit_", "")
            plan = await get_seller_plan(owner_id, plan_id)
            if not plan:
                await query.edit_message_text("❌ Plan not found.", reply_markup=self._plans_admin_menu())
                return
            context.user_data.clear()
            context.user_data["seller_waiting_plan_edit"] = plan_id
            await query.edit_message_text(
                "✏️ Edit Plan\n\nSend new values in this format:\nPlan Name | Duration | Price\n\n"
                f"Current:\n{plan.get('name')} | {plan.get('duration_text')} | {plan.get('price'):g}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="seller_plan_list")]]),
            )
            return
        if action.startswith("seller_plan_delete_"):
            plan_id = action.replace("seller_plan_delete_", "")
            deleted = await delete_seller_plan(owner_id, plan_id)
            await query.edit_message_text("✅ Plan deleted." if deleted else "❌ Plan not found.", reply_markup=self._plans_admin_menu())
            return
        if action.startswith("seller_plan_toggle_"):
            plan_id = action.replace("seller_plan_toggle_", "")
            plan = await get_seller_plan(owner_id, plan_id)
            if not plan:
                await query.edit_message_text("❌ Plan not found.", reply_markup=self._plans_admin_menu())
                return
            await set_seller_plan_active(owner_id, plan_id, not bool(plan.get("active")))
            await query.edit_message_text("✅ Plan status updated.", reply_markup=self._plans_admin_menu())
            return

        if action == "seller_admin_channels":
            text = "📢 Channel/Group management will be connected next."
        elif action == "seller_admin_payment":
            text = "💳 Seller payment settings will be connected next."
        elif action == "seller_admin_settings":
            text = "⚙️ Seller bot settings will be connected next."
        elif action == "seller_admin_stats":
            plans = await get_all_seller_plans(owner_id)
            text = f"📊 Seller Statistics\n\n📦 Total Plans: {len(plans)}\n✅ Active Plans: {sum(1 for p in plans if p.get('active'))}"
        else:
            text = "This seller admin option is being connected."
        await query.edit_message_text(text, reply_markup=self._admin_menu())

    async def _seller_admin_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = int(context.application.bot_data["seller_owner_id"])
        if update.effective_user.id != owner_id:
            return
        if context.user_data.get("seller_waiting_plan_add"):
            try:
                name, duration_text, duration_minutes, price = self._parse_plan_input(update.effective_message.text)
                plan = await create_seller_plan(owner_id, name, duration_text, duration_minutes, price)
                context.user_data.clear()
                await update.effective_message.reply_text(
                    f"✅ Plan added successfully!\n\nName: {plan['name']}\nDuration: {plan['duration_text']}\nPrice: ₹{plan['price']:g}",
                    reply_markup=self._plans_admin_menu(),
                )
            except Exception as exc:
                await update.effective_message.reply_text(
                    "❌ Invalid format.\n\nUse:\nPlan Name | Duration | Price\n\nExample:\nPremium | 30d | 199\n\n"
                    f"Error: {exc}"
                )
            return
        plan_id = context.user_data.get("seller_waiting_plan_edit")
        if plan_id:
            try:
                name, duration_text, duration_minutes, price = self._parse_plan_input(update.effective_message.text)
                updated = await update_seller_plan(owner_id, plan_id, name, duration_text, duration_minutes, price)
                context.user_data.clear()
                await update.effective_message.reply_text("✅ Plan updated successfully!" if updated else "❌ Plan not found.", reply_markup=self._plans_admin_menu())
            except Exception as exc:
                await update.effective_message.reply_text(
                    "❌ Invalid format.\n\nUse:\nPlan Name | Duration | Price\n\nExample:\nPremium | 30d | 199\n\n"
                    f"Error: {exc}"
                )
            return

    def _build_child_application(self, token: str, owner_id: int) -> Application:
        app = Application.builder().token(token).build()
        app.bot_data["seller_owner_id"] = owner_id
        app.add_handler(CommandHandler("start", self._child_start))
        app.add_handler(CommandHandler("admin", self._child_admin))
        app.add_handler(CallbackQueryHandler(self._child_menu_callback, pattern=r"^child_"))
        app.add_handler(CallbackQueryHandler(self._seller_admin_callback, pattern=r"^seller_"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._seller_admin_text))
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
                await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
                self._running[owner_id] = RunningSellerBot(owner_id, int(record["bot_id"]), app)
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
        for owner_id in list(self._running.keys()):
            await self.stop_bot(owner_id, runtime_status="service_stopped")


bot_manager = SellerBotManager()
