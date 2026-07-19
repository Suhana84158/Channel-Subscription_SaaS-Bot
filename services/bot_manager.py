import asyncio
import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from utils.timezone_ui import timezone_guide, timezone_keyboard, timezone_from_key, normalize_timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict, InvalidToken, TelegramError
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database.seller_subscriptions import (
    effective_plan, plan_limit_warning, current_plan_text, get_config,
    seller_access_state, usage_warning, bot_runtime_allowed,
)
from database.payment_gateways import (
    SUPPORTED_GATEWAYS, create_gateway_transaction, get_gateway_config,
    save_gateway_config, set_gateway_preferences, gateway_history,
)
from services.payment_gateways import create_checkout, test_gateway_connection, GatewayError
from database.seller_bots import (
    claim_runtime_recovery, finish_runtime_recovery, get_all_active_bots, get_bot,
    get_bot_by_bot_id, get_bot_by_data_owner_id, get_decrypted_bot_token,
    recovery_allowed, set_runtime_status,
)
from database.mongo import get_database
from database.seller_referrals import seller_referral_stats
from database.live_support import (
    count_support_blocks, delete_support_topic, get_live_support_settings,
    get_private_message_link, get_support_topic, get_topic_by_thread,
    is_support_blocked, save_private_message_link, save_support_topic,
    set_support_block, update_live_support_settings,
    list_support_templates, get_support_template, save_support_template,
    delete_support_template,
)
from database.platform_features import (
    audit,
    broadcast_cancel_requested,
    claim_failed_delivery,
    claim_scheduled_broadcast,
    create_coupon,
    create_invoice,
    get_failed_deliveries,
    get_policy,
    list_coupons,
    pending_scheduled_broadcasts,
    release_failed_delivery_claim,
    release_scheduled_broadcast,
    reserve_payment_fingerprint,
    resolve_failed_delivery,
    save_failed_delivery,
    save_scheduled_broadcast,
    set_scheduled_status,
)
from database.seller_data import (
    activate_subscription, active_subscriptions, add_channel, create_payment, create_plan, delete_plan,
    ensure_seller_defaults, expired_subscriptions, get_channels, get_payment,
    get_plan, get_plans, get_seller_settings, get_subscription, get_user, mark_expired,
    payment_history, pending_payments, remove_channel, set_payment_status,
    claim_payment_for_processing, finalize_processed_payment,
    release_processing_payment,
    set_seller_setting, stats, update_plan, upsert_user,
    register_referral, count_all_referrals, count_successful_referrals,
    mark_referral_rewarded, finalize_referral_reward,
    release_referral_reward, get_user_by_username, set_user_ban,
    remove_subscription,
)

logger=logging.getLogger(__name__)
WELCOME_RUNTIME_VERSION="2026-07-13-main-role-dashboard-fix-13"
MAIN_BOT_USERNAME=os.getenv("MAIN_BOT_USERNAME","Local_supplier3_bot").lstrip("@")


def _format_auto_delete(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds == 0:
        return "Off"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _template_auto_delete_seconds(template: dict) -> int:
    if not template:
        return 0
    if template.get("auto_delete_seconds") is not None:
        return max(0, int(template.get("auto_delete_seconds") or 0))
    return max(0, int(template.get("auto_delete_minutes") or 0) * 60)


def _parse_auto_delete_duration(value: str) -> int:
    raw = str(value or "").strip().lower().replace(" ", "")
    if raw in {"0", "off", "none", "disable", "disabled"}:
        return 0
    units = (("mo", 30 * 86400), ("min", 60), ("s", 1), ("m", 60), ("h", 3600), ("d", 86400))
    for suffix, multiplier in units:
        if raw.endswith(suffix):
            number = raw[:-len(suffix)]
            if not number or not number.isdigit():
                break
            seconds = int(number) * multiplier
            if seconds < 0 or seconds > 7 * 86400:
                raise ValueError("Duration 0 seconds se 7 days ke beech rakho")
            return seconds
    raise ValueError("Use: 30s, 2m, 1h, 6h, 1d ya off")

from services.message_moderation import moderate_seller_message
from handlers.deleting_messages import deleting_messages_handlers
from services.protected_bot import ProtectedExtBot
from handlers.content_protection import content_protection_handlers
from database.content_protection import get_content_protection_settings

from database.subscription_guard import save_invite, active_invites_for_user, deactivate_invite
from database.staff import active_staff, list_staff, promote_staff, remove_staff, set_staff_status, log_staff_action
from services.subscription_guard import subscription_guard_chat_member, subscription_guard_new_members
from handlers.subscription_guard import subscription_guard_handlers

@dataclass
class RunningSellerBot:
    owner_id:int; bot_id:int; application:Application

class SellerBotManager:
    def __init__(self):
        self._running: Dict[int, RunningSellerBot] = {}
        self._bot_locks: Dict[int, asyncio.Lock] = {}
        self._restore_semaphore = asyncio.Semaphore(3)
        self._watchdog_lock = asyncio.Lock()
        self._recovery_attempts: Dict[int, int] = {}
        self._recovery_totals: Dict[int, int] = {}
        self._last_recovery_at: Dict[int, datetime] = {}
        self._last_failure_at: Dict[int, datetime] = {}
        self._last_recovery_error: Dict[int, str] = {}

    def _lock_for(self, bot_id: int) -> asyncio.Lock:
        bot_id = int(bot_id)
        lock = self._bot_locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_id] = lock
        return lock
    def is_running(self,owner_id:int)->bool:return owner_id in self._running
    def get_running(self,owner_id:int):
        owner_id=int(owner_id)
        direct=self._running.get(owner_id)
        if direct:return direct
        return next((r for r in self._running.values() if int(r.owner_id)==owner_id),None)

    @staticmethod
    def main_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Plans",callback_data="c_plans"),InlineKeyboardButton("💳 Buy",callback_data="c_buy")],
            [InlineKeyboardButton("👤 My Profile",callback_data="c_profile"),InlineKeyboardButton("🔄 Renew",callback_data="c_renew")],
            [InlineKeyboardButton("🎁 Referral",callback_data="c_referral"),InlineKeyboardButton("📞 Support",callback_data="c_support")],
        ])
    @staticmethod
    def admin_menu():
        """Compact clone-bot seller panel. Existing callbacks are preserved."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Seller Profile", callback_data="a_seller_profile")],
            [InlineKeyboardButton("📦 Manage Plans", callback_data="a_plans"), InlineKeyboardButton("💳 Payment Settings", callback_data="a_payment")],
            [InlineKeyboardButton("📨 Pending Payments", callback_data="a_pending"), InlineKeyboardButton("📜 Payment History", callback_data="a_history")],
            [InlineKeyboardButton("📢 Channels / Groups", callback_data="a_channels"), InlineKeyboardButton("⚙️ Bot Settings", callback_data="a_settings")],
            [InlineKeyboardButton("👥 User Management", callback_data="a_users"), InlineKeyboardButton("👮 Staff Management", callback_data="a_staff")],
            [InlineKeyboardButton("📣 Broadcast", callback_data="a_broadcast"), InlineKeyboardButton("📊 Statistics", callback_data="a_stats")],
            [InlineKeyboardButton("🗑 Deleting Messages", callback_data="dm_home"), InlineKeyboardButton("🔒 Content Protection", callback_data="cp_home")],
            [InlineKeyboardButton("💬 Live Support", callback_data="a_live_support")],
            [InlineKeyboardButton("🛡 Subscription Guard", callback_data="sg_home")],
            [InlineKeyboardButton("🗓 Scheduled", callback_data="a_broadcast_schedule"), InlineKeyboardButton("🎟 Coupons", callback_data="a_coupons")],
            [InlineKeyboardButton("🔁 Retry Failed", callback_data="a_retry_failed"), InlineKeyboardButton("🤝 Seller Referral", callback_data="a_seller_referral")],
            [InlineKeyboardButton("📜 Terms & Policy", callback_data="a_terms")],
            [InlineKeyboardButton("🆘 Help & Commands", callback_data="a_help")],
        ])

    async def admin_panel_text(self, owner_id:int, seller_user=None):
        """Build the live summary shown above the clone-bot admin buttons."""
        try:
            plan, _assignment = await effective_plan(owner_id)
            bot_record = await get_bot_by_data_owner_id(owner_id) or {}
            settings = await get_seller_settings(owner_id)
            db = get_database()
            now_utc = datetime.now(timezone.utc)
            try:
                local_tz = ZoneInfo(settings.get("timezone") or "Asia/Kolkata")
            except (ZoneInfoNotFoundError, ValueError):
                local_tz = ZoneInfo("Asia/Kolkata")
            local_now = now_utc.astimezone(local_tz)
            local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_utc = local_start.astimezone(timezone.utc)

            active_users = await db["seller_subscriptions"].count_documents({
                "owner_id": owner_id,
                "active": True,
                "expiry_date": {"$gt": now_utc},
            })
            revenue_rows = await db["seller_payments"].aggregate([
                {"$match": {
                    "owner_id": owner_id,
                    "status": "approved",
                    "$or": [
                        {"processed_at": {"$gte": start_utc}},
                        {"updated_at": {"$gte": start_utc}},
                        {"created_at": {"$gte": start_utc}},
                    ],
                }},
                {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
            ]).to_list(length=1)
            today_revenue = revenue_rows[0].get("total", 0) if revenue_rows else 0

            seller_username = getattr(seller_user, "username", None)
            seller_label = f"@{seller_username}" if seller_username else str(getattr(seller_user, "full_name", None) or owner_id)
            clone_username = (bot_record.get("bot_username") or "Not configured").lstrip("@")
            clone_label = f"@{clone_username}" if clone_username != "Not configured" else clone_username
            currency = settings.get("currency") or "INR"
            symbol = "₹" if str(currency).upper() == "INR" else f"{currency} "
            runtime_status = str(bot_record.get("runtime_status") or "").lower()
            online = self.is_running(owner_id) or runtime_status in {"running", "online", "started"}
            status_text = "🟢 Online" if online else "🔴 Offline"

            return (
                "🛠 <b>ADMIN PANEL</b>\n\n"
                f"👤 Seller: <b>{html.escape(seller_label)}</b>\n"
                f"🤖 Clone Bot: <b>{html.escape(clone_label)}</b>\n"
                f"💎 Plan: <b>{html.escape(str(plan.get('name', 'Free')))}</b>\n"
                f"👥 Active Users: <b>{active_users:,}</b>\n"
                f"💰 Today Revenue: <b>{symbol}{float(today_revenue or 0):,.2f}</b>\n"
                f"{status_text}"
            )
        except Exception:
            logger.exception("Failed to build seller admin summary owner=%s", owner_id)
            return "🛠 <b>ADMIN PANEL</b>\n\n⚠️ Live summary is temporarily unavailable."

    @staticmethod
    def back(target="a_home"): return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back",callback_data=target)]])
    @staticmethod
    def limit_keyboard(back_target="a_home"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Upgrade Plan", callback_data="seller_upgrade_plan")],
            [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
            [InlineKeyboardButton("❌ Close", callback_data=back_target)],
        ])

    @staticmethod
    def plans_admin_menu():
        return InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Plan",callback_data="a_plan_add")],[InlineKeyboardButton("📋 View Plans",callback_data="a_plan_list")],[InlineKeyboardButton("⬅ Back",callback_data="a_home")]])
    @staticmethod
    def channels_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Channel/Group",callback_data="a_channel_add")],
            [InlineKeyboardButton("📋 Channel List",callback_data="a_channel_list")],
            [InlineKeyboardButton(
                "🔗 Resend Invite Links to Active Subscribers",
                callback_data="a_channel_resend",
            )],
            [InlineKeyboardButton("⬅ Back",callback_data="a_home")],
        ])
    @staticmethod
    def payment_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Automatic Gateways",callback_data="a_pg_home")],
            [InlineKeyboardButton("🏦 Set UPI ID",callback_data="a_set_upi_id")],
            [InlineKeyboardButton("👤 Set UPI Name",callback_data="a_set_upi_name")],
            [InlineKeyboardButton("🖼 Upload QR",callback_data="a_set_qr")],
            [InlineKeyboardButton("⬅ Back",callback_data="a_home")],
        ])
    @staticmethod
    def live_support_menu(settings):
        enabled=bool(settings.get("enabled"))
        mode=settings.get("mode","topic")
        group_title=settings.get("support_group_title") or "Not connected"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔴 Turn Support OFF" if enabled else "🟢 Turn Support ON",
                callback_data="a_live_support_toggle",
            )],
            [InlineKeyboardButton(
                ("✅ " if mode=="private" else "")+"💬 Normal Private Reply",
                callback_data="a_live_support_mode_private",
            )],
            [InlineKeyboardButton(
                ("✅ " if mode=="topic" else "")+"🧵 Topic Mode",
                callback_data="a_live_support_mode_topic",
            )],
            [InlineKeyboardButton(f"📌 Group: {group_title[:28]}",callback_data="a_live_support_group_info")],
            [InlineKeyboardButton("⚡ Reply Templates",callback_data="a_support_templates")],
            [InlineKeyboardButton("🚫 Blocked Users Count",callback_data="a_live_support_blocks")],
            [InlineKeyboardButton("⬅ Back",callback_data="a_home")],
        ])

    @staticmethod
    def live_support_text(settings, blocked_count):
        mode_name="Topic Mode" if settings.get("mode","topic")=="topic" else "Normal Private Reply"
        group_name=settings.get("support_group_title") or "Not connected"
        return (
            "💬 Live Support Settings\n\n"
            f"Status: {'🟢 ON' if settings.get('enabled') else '🔴 OFF'}\n"
            f"Reply Mode: {mode_name}\n"
            f"Support Group: {group_name}\n"
            f"Blocked Users: {blocked_count}\n\n"
            "Topic Mode me har user ke liye ek permanent topic banta hai. "
            "Messages auto-delete nahi honge.\n\n"
            "Connect Support Group\n\n"
            "1. Private supergroup banao.\n"
            "2. Topics ON karo.\n"
            "3. Clone Bot ko Admin banao.\n"
            "4. Manage Topics permission ON rakho.\n"
            "5. Usi group me /connectsupport bhejo.\n\n"
            "Connect hone ke baad har user ka alag topic automatically banega."
        )

    @staticmethod
    def support_templates_menu(templates):
        rows=[[InlineKeyboardButton("➕ Add Command",callback_data="a_support_tpl_add")]]
        for item in templates:
            command=item.get("command","")
            duration=_format_auto_delete(_template_auto_delete_seconds(item))
            icon="🔴" if duration=="Off" else "🟢"
            rows.append([InlineKeyboardButton(f"/{command}   {icon} {duration}",callback_data=f"a_support_tpl_view_{command}")])
        rows.append([InlineKeyboardButton("⬅ Back",callback_data="a_live_support")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def support_template_edit_menu(command):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Edit Text",callback_data=f"a_support_tpl_text_{command}"),InlineKeyboardButton("🗑 Remove Text",callback_data=f"a_support_tpl_rmtext_{command}")],
            [InlineKeyboardButton("🖼 Edit Media",callback_data=f"a_support_tpl_media_{command}"),InlineKeyboardButton("🗑 Remove Media",callback_data=f"a_support_tpl_rmmedia_{command}")],
            [InlineKeyboardButton("🔗 Edit Buttons",callback_data=f"a_support_tpl_buttons_{command}"),InlineKeyboardButton("🗑 Remove Buttons",callback_data=f"a_support_tpl_rmbuttons_{command}")],
            [InlineKeyboardButton("⏱ Template Auto Remove",callback_data=f"a_support_tpl_autodel_{command}")],
            [InlineKeyboardButton("👀 Preview",callback_data=f"a_support_tpl_preview_{command}")],
            [InlineKeyboardButton("🗑 Delete Command",callback_data=f"a_support_tpl_delete_{command}")],
            [InlineKeyboardButton("⬅ Back",callback_data="a_support_templates")],
        ])

    @staticmethod
    def support_template_auto_delete_menu(command, current_seconds=0):
        current=_format_auto_delete(current_seconds)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Off",callback_data=f"a_tpl_ad_0_{command}"),InlineKeyboardButton("30 Seconds",callback_data=f"a_tpl_ad_30_{command}")],
            [InlineKeyboardButton("1 Minute",callback_data=f"a_tpl_ad_60_{command}"),InlineKeyboardButton("5 Minutes",callback_data=f"a_tpl_ad_300_{command}")],
            [InlineKeyboardButton("10 Minutes",callback_data=f"a_tpl_ad_600_{command}"),InlineKeyboardButton("30 Minutes",callback_data=f"a_tpl_ad_1800_{command}")],
            [InlineKeyboardButton("1 Hour",callback_data=f"a_tpl_ad_3600_{command}"),InlineKeyboardButton("⌨️ Custom",callback_data=f"a_tpl_ad_custom_{command}")],
            [InlineKeyboardButton(f"Current: {current}",callback_data="noop")],
            [InlineKeyboardButton("⬅ Back",callback_data=f"a_support_tpl_view_{command}")],
        ])

    @staticmethod
    def settings_menu():
        return InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Bot Name",callback_data="a_set_bot_name")],[InlineKeyboardButton("💬 Welcome Message",callback_data="a_welcome")],[InlineKeyboardButton("📞 Support Username",callback_data="a_set_support")],[InlineKeyboardButton("💵 Currency",callback_data="a_set_currency"),InlineKeyboardButton("🕒 Timezone",callback_data="a_set_timezone")],[InlineKeyboardButton("🔔 Reminder Days",callback_data="a_set_reminder")],[InlineKeyboardButton("🎁 Referral Reward Days",callback_data="a_set_referral_days")],[InlineKeyboardButton("⬅ Back",callback_data="a_home")]])

    @staticmethod
    def welcome_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Edit Text",callback_data="a_welcome_text"),InlineKeyboardButton("🗑 Remove Text",callback_data="a_welcome_remove_text")],
            [InlineKeyboardButton("🖼 Edit Media",callback_data="a_welcome_media"),InlineKeyboardButton("🗑 Remove Media",callback_data="a_welcome_remove_media")],
            [InlineKeyboardButton("🔗 Edit Buttons",callback_data="a_welcome_buttons"),InlineKeyboardButton("🗑 Remove Buttons",callback_data="a_welcome_remove_buttons")],
            [InlineKeyboardButton("👀 Preview",callback_data="a_welcome_preview")],
            [InlineKeyboardButton("⬅ Back",callback_data="a_settings")],
        ])

    @staticmethod
    def welcome_buttons_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Choose Bot Button",callback_data="a_welcome_quick")],
            [InlineKeyboardButton("✍ Write Manually",callback_data="a_welcome_manual")],
            [InlineKeyboardButton("👀 See Current Buttons",callback_data="a_welcome_see_buttons")],
            [InlineKeyboardButton("🧹 Remove All Buttons",callback_data="a_welcome_remove_buttons")],
            [InlineKeyboardButton("⬅ Back",callback_data="a_welcome")],
        ])

    @staticmethod
    def welcome_quick_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Plans",callback_data="a_wq_plans"),InlineKeyboardButton("💳 Buy",callback_data="a_wq_buy")],
            [InlineKeyboardButton("👤 My Profile",callback_data="a_wq_profile"),InlineKeyboardButton("🔄 Renew",callback_data="a_wq_renew")],
            [InlineKeyboardButton("🎁 Referral",callback_data="a_wq_referral"),InlineKeyboardButton("📞 Support",callback_data="a_wq_support")],
            [InlineKeyboardButton("🏠 Main Menu",callback_data="a_wq_home")],
            [InlineKeyboardButton("⬅ Back",callback_data="a_welcome_buttons")],
        ])

    @staticmethod
    def personalize(text,user,bot_name="Subscription Bot"):
        from datetime import datetime as _datetime
        now=_datetime.now()
        values={
            "{ID}":str(user.id),
            "{NAME}":user.first_name or "",
            "{SURNAME}":user.last_name or "",
            "{NAMESURNAME}":" ".join(x for x in [user.first_name,user.last_name] if x),
            "{USERNAME}":("@"+user.username) if user.username else "",
            "{LANG}":user.language_code or "",
            "{DATE}":now.strftime("%d-%m-%Y"),
            "{TIME}":now.strftime("%I:%M %p"),
            "{WEEKDAY}":now.strftime("%A"),
            "{MENTION}":user.mention_html(),
            "{BOTNAME}":bot_name,
        }
        result=text or ""
        for key,value in values.items(): result=result.replace(key,value)
        return result

    @staticmethod
    def parse_welcome_buttons(text):
        rows=[]
        for raw_line in text.splitlines():
            raw_line=raw_line.strip()
            if not raw_line: continue
            row=[]
            for item in raw_line.split("&&"):
                item=item.strip()
                if " - " not in item: raise ValueError("Use: Button title - URL")
                title,target=[x.strip() for x in item.split(" - ",1)]
                if not title or not target: raise ValueError("Button title and target required")
                if target.startswith(("http://","https://","tg://")) or target.startswith("t.me/"):
                    if target.startswith("t.me/"): target="https://"+target
                    row.append({"text":title,"type":"url","value":target})
                elif target.startswith("feature:"):
                    feature=target.split(":",1)[1].lower()
                    allowed={"plans":"c_plans","buy":"c_buy","profile":"c_profile","renew":"c_renew","referral":"c_referral","support":"c_support","home":"c_home"}
                    if feature not in allowed: raise ValueError("Unknown feature button")
                    row.append({"text":title,"type":"callback","value":allowed[feature]})
                else:
                    raise ValueError("Target must be URL or feature:plans/buy/profile/renew/referral/support/home")
            if row: rows.append(row)
        if not rows: raise ValueError("No buttons found")
        return rows

    @staticmethod
    def build_welcome_keyboard(rows):
        if not rows: return None
        keyboard=[]
        for row in rows:
            built=[]
            for item in row:
                if item.get("type")=="url": built.append(InlineKeyboardButton(item.get("text","Button"),url=item.get("value")))
                else: built.append(InlineKeyboardButton(item.get("text","Button"),callback_data=item.get("value","c_home")))
            if built: keyboard.append(built)
        return InlineKeyboardMarkup(keyboard) if keyboard else None

    async def send_welcome(self,message,context,settings,user):
        # Seller ka editable welcome text optional hai. Agar seller text remove
        # kare, tab bhi default welcome title aur permanent SaaS branding dikhegi.
        seller_text=(settings.get("welcome_message") or "").strip()
        if seller_text:
            welcome_text=self.personalize(
                seller_text,
                user,
                settings.get("bot_name","Subscription Bot"),
            )
        else:
            welcome_text="👋 WELCOME TO OUR SUBSCRIPTION BOT"

        # Ye branding seller edit/remove nahi kar sakta. Main bot username
        # Render ke MAIN_BOT_USERNAME environment variable se aata hai.
        creator_line=(
            "\n\n🤖 Powered by "
            f'<a href="https://t.me/{MAIN_BOT_USERNAME}">'
            f"@{MAIN_BOT_USERNAME}</a>"
        )
        text=f"{welcome_text}{creator_line}"

        # Seller ke welcome buttons fully removable hain. Empty list ka matlab
        # welcome message ke niche koi button nahi dikhana.
        keyboard=self.build_welcome_keyboard(
            settings.get("welcome_buttons") or []
        )
        media_type=settings.get("welcome_media_type")
        file_id=settings.get("welcome_media_file_id")

        async def send(parse_mode="HTML"):
            kwargs={"reply_markup":keyboard}
            if parse_mode:
                kwargs["parse_mode"]=parse_mode
            if file_id and media_type=="photo":
                return await message.reply_photo(file_id,caption=text,**kwargs)
            if file_id and media_type=="video":
                return await message.reply_video(file_id,caption=text,**kwargs)
            if file_id and media_type=="animation":
                return await message.reply_animation(file_id,caption=text,**kwargs)
            if file_id and media_type=="document":
                return await message.reply_document(file_id,caption=text,**kwargs)
            return await message.reply_text(
                text,
                disable_web_page_preview=True,
                **kwargs,
            )

        try:
            return await send("HTML")
        except BadRequest as exc:
            logger.warning("Welcome HTML/media send failed; retrying plain text: %s",exc)
            try:
                return await send(None)
            except BadRequest:
                # If an old/invalid Telegram file_id is stored, remove media and send text.
                if file_id:
                    await set_seller_setting(self.owner(context),"welcome_media_type","")
                    await set_seller_setting(self.owner(context),"welcome_media_file_id","")
                    settings["welcome_media_type"]=""
                    settings["welcome_media_file_id"]=""
                    return await message.reply_text(
                        text,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                raise

    @staticmethod
    def format_dt(value):
        if not value:
            return "-"
        try:
            return value.astimezone(timezone.utc).strftime("%d-%m-%Y %I:%M:%S %p UTC")
        except Exception:
            return str(value)

    async def user_details_text(self,owner,user_id):
        user=await get_user(owner,int(user_id))
        sub=await get_subscription(owner,int(user_id))

        if not user:
            return None,None,None

        username=f"@{user.get('username')}" if user.get("username") else "Not set"
        name=" ".join(
            value for value in [user.get("first_name"),user.get("last_name")]
            if value
        ) or "Unknown"

        now=datetime.now(timezone.utc)
        expiry=(sub or {}).get("expiry_date")
        if expiry and expiry.tzinfo is None:
            expiry=expiry.replace(tzinfo=timezone.utc)
        active=bool(sub and sub.get("active") and expiry and expiry>now)
        if sub and expiry:
            sub["expiry_date"]=expiry

        text=(
            "👤 User Details\n\n"
            f"🆔 ID: {user.get('user_id')}\n"
            f"👤 Name: {name}\n"
            f"📝 Username: {username}\n"
            f"🚫 Banned: {'Yes' if user.get('banned') else 'No'}\n"
            f"📋 Reason: {user.get('ban_reason') or '-'}\n"
            f"📅 Joined: {self.format_dt(user.get('joined_at'))}\n\n"
            f"💎 Plan: {(sub or {}).get('plan') or 'No Plan'}\n"
            f"📅 Expiry: {self.format_dt((sub or {}).get('expiry_date'))}\n"
            f"📌 Status: {'Active' if active else 'No Subscription'}"
        )
        return text,user,sub

    async def show_user_details(self,q,owner,user_id):
        text,user,sub=await self.user_details_text(owner,user_id)

        if not user:
            await q.edit_message_text(
                "❌ User not found.",
                reply_markup=self.back("a_users"),
            )
            return

        banned=bool(user.get("banned"))
        keyboard=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Give Subscription",callback_data=f"a_user_give_{user_id}")],
            [InlineKeyboardButton("⌛ Extend Subscription",callback_data=f"a_user_extend_{user_id}")],
            [InlineKeyboardButton("❌ Remove Subscription",callback_data=f"a_user_remove_{user_id}")],
            [InlineKeyboardButton(
                "✅ Unban User" if banned else "🚫 Ban User",
                callback_data=f"a_user_unban_{user_id}" if banned else f"a_user_ban_{user_id}",
            )],
            [InlineKeyboardButton("⬅ Back",callback_data="a_users")],
        ])

        await q.edit_message_text(text,reply_markup=keyboard)

    async def show_admin_plan_selector(self,q,owner,user_id,mode):
        plans=await get_plans(owner,True)

        if not plans:
            await q.edit_message_text(
                "❌ No active plans available.",
                reply_markup=self.back(f"a_user_view_{user_id}"),
            )
            return

        title="🎁 Choose plan to give" if mode=="give" else "⌛ Choose plan duration to extend"
        kb=[]

        for plan in plans:
            kb.append([InlineKeyboardButton(
                f"{plan['name']} — {plan['duration_text']} — ₹{plan['price']:g}",
                callback_data=f"a_user_apply_{mode}_{user_id}_{plan['plan_id']}",
            )])

        kb.append([InlineKeyboardButton("⬅ Back",callback_data=f"a_user_view_{user_id}")])
        await q.edit_message_text(title,reply_markup=InlineKeyboardMarkup(kb))

    async def payment_details_caption(
        self,
        owner,
        payment,
        status=None,
        processed_by=None,
    ):
        user=await get_user(owner,int(payment["user_id"])) or {}

        name=" ".join(
            value for value in [
                user.get("first_name"),
                user.get("last_name"),
            ] if value
        ) or "Unknown"

        username=(
            f"@{user.get('username')}"
            if user.get("username")
            else "Not set"
        )

        created=payment.get("created_at")
        created_text=self.format_dt(created)
        current_status=status or payment.get("status","pending")

        status_icon={
            "pending":"🟡",
            "approved":"✅",
            "rejected":"❌",
        }.get(current_status,"ℹ️")

        lines=[
            f"{status_icon} Payment {current_status.title()}",
            "",
            f"🧾 Payment ID: {payment.get('payment_id')}",
            f"🆔 User ID: {payment.get('user_id')}",
            f"👤 Name: {name}",
            f"📝 Username: {username}",
            f"📦 Plan: {payment.get('plan')}",
            f"⏳ Duration: {payment.get('duration_text') or '-'}",
            f"💰 Amount: ₹{payment.get('amount',0):g}",
            f"📅 Submitted: {created_text}",
            f"📌 Status: {current_status.title()}",
        ]

        if processed_by:
            lines.extend([
                f"👮 Processed By: {processed_by}",
                f"🕒 Processed At: {self.format_dt(datetime.now(timezone.utc))}",
            ])

        return "\n".join(lines)

    @staticmethod
    def parse_duration(value:str)->int:
        value=value.strip().lower(); n=int(value[:-1]); unit=value[-1]
        if n<=0: raise ValueError("Duration must be positive")
        if unit=="m": return n
        if unit=="h": return n*60
        if unit=="d": return n*1440
        raise ValueError("Use m, h or d")
    @classmethod
    def parse_plan(cls,text:str):
        p=[x.strip() for x in text.split("|")]
        if len(p)!=3: raise ValueError("Use: Plan Name | Duration | Price")
        return p[0],p[1].lower(),cls.parse_duration(p[1]),float(p[2])

    def owner(self,context): return int(context.application.bot_data["seller_owner_id"])
    def seller_account(self,context): return int(context.application.bot_data.get("seller_account_id", self.owner(context)))
    async def staff_record(self, update, context):
        uid = int(update.effective_user.id)
        if uid == self.seller_account(context):
            return {"role": "seller", "status": "active", "permissions": ["*"]}
        return await active_staff(self.owner(context), uid)

    async def auth(self,update,context):
        return bool(await self.staff_record(update, context))

    @staticmethod
    def staff_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Promote Admin", callback_data="a_staff_add_admin"), InlineKeyboardButton("➕ Promote Moderator", callback_data="a_staff_add_moderator")],
            [InlineKeyboardButton("📋 Staff List", callback_data="a_staff_list")],
            [InlineKeyboardButton("⬅ Back", callback_data="a_home")],
        ])

    @staticmethod
    def staff_item_menu(user_id:int, suspended:bool=False):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Activate" if suspended else "⏸ Suspend", callback_data=f"a_staff_status_{user_id}_{'active' if suspended else 'suspended'}")],
            [InlineKeyboardButton("❌ Remove Staff", callback_data=f"a_staff_remove_{user_id}")],
            [InlineKeyboardButton("⬅ Staff List", callback_data="a_staff_list")],
        ])

    async def safe_query_message(self,q,text,reply_markup=None):
        """Edit text messages; reply with a new message when the button is on media."""
        try:
            return await q.edit_message_text(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            error=str(exc).lower()
            if (
                "there is no text in the message to edit" in error
                or "message can't be edited" in error
                or "message is not modified" in error
            ):
                if "message is not modified" in error:
                    return None
                return await q.message.reply_text(
                    text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            raise

    async def child_start(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)

        # Clone-bot seller opens the selected section directly from main-bot deep links.
        staff = await self.staff_record(update, context)
        if staff:
            context.user_data.clear()
            target = context.args[0] if context.args else "admin_panel"
            if target == "admin_payment":
                settings = await get_seller_settings(owner)
                await update.effective_message.reply_text(
                    f"💳 Payment Settings\n\nUPI Name: {settings.get('upi_name') or 'Not Set'}\n"
                    f"UPI ID: {settings.get('upi_id') or 'Not Set'}\n"
                    f"QR: {'Added' if settings.get('upi_qr_file_id') else 'Not Added'}",
                    reply_markup=self.payment_menu(),
                )
            elif target == "admin_settings":
                settings = await get_seller_settings(owner)
                await update.effective_message.reply_text(
                    "⚙️ Bot Settings\n\n"
                    f"Bot Name: {settings.get('bot_name') or '-'}\n"
                    f"Support: {settings.get('support_username') or '-'}\n"
                    f"Currency: {settings.get('currency') or 'INR'}\n"
                    f"Timezone: {settings.get('timezone') or 'Asia/Kolkata'}",
                    reply_markup=self.settings_menu(),
                )
            elif target == "admin_channels":
                await update.effective_message.reply_text("📢 Channels / Groups", reply_markup=self.channels_menu())
            elif target == "admin_stats":
                data = await stats(owner)
                await update.effective_message.reply_text(
                    "📊 Statistics\n\n"
                    f"Users: {data.get('users',0)}\nPlans: {data.get('plans',0)}\n"
                    f"Channels/Groups: {data.get('channels',0)}\n"
                    f"Pending Payments: {data.get('pending',0)}\nRevenue: ₹{data.get('revenue',0):g}",
                    reply_markup=self.admin_menu(),
                )
            elif target == "admin_terms":
                policy = await get_policy(owner)
                parts=[]
                for key in ("terms","privacy","refund","support"):
                    value=(policy or {}).get(key)
                    if value: parts.append(f"{key.title()}:\n{value}")
                await update.effective_message.reply_text(
                    "📜 Terms & Policy\n\n" + ("\n\n".join(parts) if parts else "No policy configured."),
                    reply_markup=self.admin_menu(),
                )
            else:
                await update.effective_message.reply_text(
                    await self.admin_panel_text(owner, update.effective_user),
                    reply_markup=self.admin_menu(),
                    parse_mode="HTML",
                )
            return

        try:
            await upsert_user(owner,update.effective_user)
            user_record=await get_user(owner,update.effective_user.id)

            if user_record and user_record.get("banned"):
                await update.effective_message.reply_text(
                    "🚫 You are banned from using this bot.\n"
                    f"Reason: {user_record.get('ban_reason') or 'Not specified'}"
                )
                return

            if context.args:
                arg=context.args[0]
                if arg.startswith("ref_"):
                    try:
                        referrer_id=int(arg.replace("ref_","",1))
                        await register_referral(owner,referrer_id,update.effective_user.id)
                    except (TypeError,ValueError):
                        pass

            record=await get_bot_by_data_owner_id(owner)
            settings=await ensure_seller_defaults(
                owner,
                (record or {}).get("bot_name","Subscription Bot"),
            )
            await self.send_welcome(
                update.effective_message,
                context,
                settings,
                update.effective_user,
            )
        except Exception as exc:
            logger.exception(
                "Child /start failed owner=%s runtime=%s",
                owner,
                WELCOME_RUNTIME_VERSION,
            )
            await update.effective_message.reply_text(
                "❌ Welcome message could not be sent.\n"
                f"Runtime: {WELCOME_RUNTIME_VERSION}\n"
                f"Error: {str(exc)[:250]}"
            )

    async def help_command(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)
        is_owner=update.effective_user.id==owner

        user_text=(
            "📚 Clone Bot Help Center\n\n"
            "👤 User Commands\n"
            "/start — Open the welcome menu\n"
            "/help — Open this help guide\n"
            "/version — Check deployed runtime version\n\n"
            "📋 Plans & Purchase\n"
            "Open Plans or Buy Plan, select a plan, complete payment and upload the payment screenshot when manual payment is enabled.\n\n"
            "🔄 Renew Plan\n"
            "Renew before or after expiry using the available renewal options.\n\n"
            "👤 My Profile\n"
            "View your Telegram ID, active plan, start date, expiry, remaining time and referral details.\n\n"
            "🎁 Referral\n"
            "Share your referral link. Reward days are added according to the seller's referral settings after a valid approved payment.\n\n"
            "📞 Live Support\n"
            "Send your message or supported media through the Support button. The seller's reply will return inside this bot.\n\n"
            "⏰ Expiry\n"
            "Expired access is removed automatically. Use Renew Plan to continue."
        )

        if not is_owner:
            await update.effective_message.reply_text(user_text)
            return

        admin_text=(
            user_text
            + "\n\n━━━━━━━━━━━━━━━━━━━━\n"
            "🛠 Seller Admin Help & Commands\n\n"
            "/admin — Open the Admin Panel\n"
            "/connectgroup — Connect a private subscription group\n"
            "/connectsupport — Connect the Live Support forum group\n\n"
            "🚀 Quick Setup\n"
            "1. Create plans\n"
            "2. Connect channel/group\n"
            "3. Configure payments\n"
            "4. Edit and preview welcome message\n"
            "5. Test purchase, approval and invite delivery\n\n"
            "📦 Manage Plans\n"
            "Add, edit, enable, disable or delete user plans. Example input: Premium | 30d | 199\n\n"
            "📂 Channels / Groups\n"
            "Add the bot as admin with Invite Users and Ban Users permissions. For private groups, send /connectgroup inside the group or use: -1001234567890 | Group Name\n\n"
            "💳 Payments\n"
            "Set UPI ID, UPI name and QR, or configure an available automatic gateway. Review Pending Payments and Payment History.\n\n"
            "👥 User Management\n"
            "Search users, give/extend/remove subscriptions, and ban/unban accounts.\n\n"
            "💬 Welcome Editor\n"
            "Edit text, media and buttons, then use Preview. Test every custom feature and URL button.\n\n"
            "🎫 Live Support\n"
            "Connect a forum group using /connectsupport. Manage Reply Templates and Template Auto Remove from Live Support settings.\n\n"
            "📢 Broadcast\n"
            "Send now or schedule for later. Review results and retry failed deliveries when necessary.\n\n"
            "🧪 Quick Troubleshooting\n"
            "• No reply: check runtime logs and token status\n"
            "• Group issue: recheck admin permissions\n"
            "• No invite: verify Invite Users permission\n"
            "• Support issue: enable forum topics and reconnect\n"
            "• Payment issue: verify UPI/QR or gateway credentials"
        )
        await update.effective_message.reply_text(admin_text)

    async def admin(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        if not await self.auth(update,context): await update.effective_message.reply_text("❌ Not authorized"); return
        context.user_data.clear()
        await update.effective_message.reply_text(
            await self.admin_panel_text(self.owner(context), update.effective_user),
            reply_markup=self.admin_menu(),
            parse_mode="HTML",
        )

    async def show_plans(self,q,owner,select=False):
        plans=await get_plans(owner,True)
        settings=await get_seller_settings(owner)
        currency=settings.get("currency","INR")
        back_keyboard=self.back("c_home")

        if not plans:
            await self.safe_query_message(
                q,
                "📋 No plans available.",
                back_keyboard,
            )
            return

        kb=[]
        lines=["📋 Available Plans\n"]

        for p in plans:
            lines.append(
                f"• {p['name']} — {p['duration_text']} — "
                f"{currency} {p['price']:g}"
            )

            if select:
                kb.append([
                    InlineKeyboardButton(
                        f"Buy {p['name']} - {currency} {p['price']:g}",
                        callback_data=f"c_select_{p['plan_id']}",
                    )
                ])

        kb.append([
            InlineKeyboardButton("⬅ Back",callback_data="c_home")
        ])

        await self.safe_query_message(
            q,
            "\n".join(lines),
            InlineKeyboardMarkup(kb),
        )

    async def child_callback(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        q=update.callback_query
        await q.answer()
        owner=self.owner(context)
        action=q.data
        if action=="seller_current_plan":
            await q.edit_message_text(await current_plan_text(owner), reply_markup=self.back("a_home")); return
        if action=="seller_upgrade_plan":
            cfg=await get_config()
            plans=[p for p in cfg.get("paid_plans",[]) if p.get("active",True)]
            if not plans:
                await q.edit_message_text("No paid seller plans are available right now.", reply_markup=self.back("a_home")); return
            lines=["💎 Upgrade Seller Plan", ""]
            for p in plans:
                lines.append(f"• {p.get('name','Plan')} — ₹{p.get('price',0)} / {p.get('duration_days',30)} days")
            lines += ["", "Contact the SaaS owner to activate a plan."]
            await q.edit_message_text("\n".join(lines), reply_markup=self.back("a_home")); return
        back_keyboard=self.back("c_home")

        if action=="c_home":
            record=await get_bot_by_data_owner_id(owner)
            settings=await ensure_seller_defaults(
                owner,
                (record or {}).get("bot_name","Subscription Bot"),
            )
            await self.send_welcome(
                q.message,
                context,
                settings,
                q.from_user,
            )
            return

        if action=="c_plans":
            await self.show_plans(q,owner,True)
            return

        if action in {"c_buy","c_renew"}:
            await self.show_plans(q,owner,True)
            return

        if action.startswith("c_select_"):
            plan=await get_plan(owner,action.replace("c_select_",""))

            if not plan:
                await q.answer("Plan not found",show_alert=True)
                return

            context.user_data["selected_child_plan"]=plan
            s=await get_seller_settings(owner)

            text=(
                "💳 Payment\n\n"
                f"Plan: {plan['name']}\n"
                f"Amount: {s.get('currency','INR')} {plan['price']:g}\n"
                f"Duration: {plan['duration_text']}\n\n"
                f"UPI Name: {s.get('upi_name') or 'Not Set'}\n"
                f"UPI ID: {s.get('upi_id') or 'Not Set'}"
            )

            gateway_cfg=await get_gateway_config("seller", owner, decrypt=True)
            rows=[]
            for gateway in SUPPORTED_GATEWAYS:
                if (gateway_cfg.get("gateways") or {}).get(gateway,{}).get("enabled"):
                    rows.append([InlineKeyboardButton(f"💳 Pay with {gateway.title()}",callback_data=f"c_pg_{gateway}_{plan['plan_id']}")])
            if gateway_cfg.get("manual_enabled",True):
                rows.append([InlineKeyboardButton("📤 Upload Payment Screenshot",callback_data="c_upload")])
            rows.append([InlineKeyboardButton("⬅ Back",callback_data="c_buy")])
            kb=InlineKeyboardMarkup(rows)

            if s.get("upi_qr_file_id") and gateway_cfg.get("manual_enabled",True):
                await q.message.reply_photo(
                    s["upi_qr_file_id"],
                    caption=text,
                    reply_markup=kb,
                )
            else:
                await self.safe_query_message(q,text,kb)
            return

        if action.startswith("c_pg_"):
            try:
                _,_,gateway,plan_id=action.split("_",3)
            except ValueError:
                await q.answer("Invalid payment option",show_alert=True); return
            plan=await get_plan(owner,plan_id)
            if not plan:
                await q.answer("Plan not found",show_alert=True); return
            tx=await create_gateway_transaction(
                scope="seller", owner_id=owner, payer_user_id=q.from_user.id,
                gateway=gateway, amount=float(plan["price"]), currency="INR",
                purpose="child_subscription", reference_id=plan_id,
                metadata={"plan_id":plan_id,"plan_name":plan["name"],"description":f"{plan['name']} subscription"},
            )
            try:
                checkout=await create_checkout(tx)
            except GatewayError as exc:
                await self.safe_query_message(q,f"❌ Gateway error: {exc}",back_keyboard); return
            await self.safe_query_message(
                q,
                f"💳 {gateway.title()} Secure Payment\n\nPlan: {plan['name']}\nAmount: ₹{plan['price']:g}\nTransaction: {tx['transaction_id']}\n\nPayment verify hote hi subscription automatically activate hogi.",
                InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay Now",url=checkout.get("checkout_url"))],[InlineKeyboardButton("⬅ Back",callback_data="c_buy")]]),
            )
            return

        if action=="c_upload":
            context.user_data["waiting_child_screenshot"]=True
            await q.message.reply_text(
                "📷 Upload your payment screenshot.",
                reply_markup=back_keyboard,
            )
            return

        if action=="c_profile":
            try:
                user_record=await get_user(owner,q.from_user.id) or {}
                sub=await get_subscription(owner,q.from_user.id)
                me=await context.bot.get_me()

                def aware_utc(value):
                    if not value:
                        return None
                    if value.tzinfo is None:
                        return value.replace(tzinfo=timezone.utc)
                    return value.astimezone(timezone.utc)

                joined=aware_utc(user_record.get("joined_at"))
                joined_text=(
                    joined.strftime("%d %b %Y, %I:%M %p UTC")
                    if joined else "Unknown"
                )

                referral_link=(
                    f"https://t.me/{me.username}"
                    f"?start=ref_{q.from_user.id}"
                )

                total_referrals=await count_all_referrals(
                    owner,
                    q.from_user.id,
                )
                successful_referrals=await count_successful_referrals(
                    owner,
                    q.from_user.id,
                )

                username=(
                    f"@{q.from_user.username}"
                    if q.from_user.username else "Not set"
                )
                full_name=" ".join(
                    value for value in [
                        q.from_user.first_name,
                        q.from_user.last_name,
                    ] if value
                ) or "Unknown"

                lines=[
                    "👤 My Profile",
                    "",
                    f"🆔 User ID: {q.from_user.id}",
                    f"👤 Name: {full_name}",
                    f"📝 Username: {username}",
                    f"🌐 Language: {q.from_user.language_code or 'Unknown'}",
                    f"📅 Joined: {joined_text}",
                    f"👥 Total Referrals: {total_referrals}",
                    f"✅ Successful Referrals: {successful_referrals}",
                    "",
                    "🔗 Referral Link:",
                    referral_link,
                    "",
                    "━━━━━━━━━━━━━━━━━━━━",
                    "📋 Subscription Details",
                ]

                now=datetime.now(timezone.utc)
                expiry=aware_utc((sub or {}).get("expiry_date"))
                active=bool(
                    sub
                    and sub.get("active")
                    and expiry
                    and expiry>now
                )

                if active:
                    remaining=expiry-now
                    days=max(remaining.days,0)
                    hours=remaining.seconds//3600
                    minutes=(remaining.seconds%3600)//60

                    start=aware_utc(
                        sub.get("start_date")
                        or sub.get("created_at")
                    )
                    start_text=(
                        start.strftime("%d %b %Y, %I:%M %p UTC")
                        if start else "Unknown"
                    )
                    expiry_text=expiry.strftime(
                        "%d %b %Y, %I:%M %p UTC"
                    )

                    amount=sub.get("amount")
                    amount_text=(
                        f"₹{amount:g}"
                        if isinstance(amount,(int,float))
                        else str(amount or "—")
                    )

                    lines.extend([
                        "📌 Status: ✅ Active",
                        f"💎 Plan: {sub.get('plan') or 'Unknown'}",
                        f"💰 Amount: {amount_text}",
                        f"⏳ Duration: {sub.get('duration_text') or '—'}",
                        f"📅 Start Date: {start_text}",
                        f"📅 Expiry: {expiry_text}",
                        f"⏱ Time Left: {days}d {hours}h {minutes}m",
                    ])
                else:
                    lines.extend([
                        "📌 Status: ❌ No Active Subscription",
                        f"💎 Last Plan: {(sub or {}).get('plan') or '—'}",
                        f"💰 Amount: {(sub or {}).get('amount') or '—'}",
                        f"⏳ Duration: {(sub or {}).get('duration_text') or '—'}",
                        f"📅 Expiry: {self.format_dt(expiry)}",
                    ])

                await self.safe_query_message(
                    q,
                    "\n".join(lines),
                    back_keyboard,
                )

            except Exception as exc:
                logger.exception(
                    "Profile failed owner=%s user=%s",
                    owner,
                    q.from_user.id,
                )
                await q.message.reply_text(
                    "❌ Profile could not be loaded.\n"
                    f"Error: {str(exc)[:250]}",
                    reply_markup=back_keyboard,
                )
            return

        if action=="c_referral":
            me=await context.bot.get_me()
            settings=await get_seller_settings(owner)
            reward_days=int(settings.get("referral_reward_days",7) or 7)
            total=await count_all_referrals(owner,q.from_user.id)
            successful=await count_successful_referrals(owner,q.from_user.id)
            referral_link=f"https://t.me/{me.username}?start=ref_{q.from_user.id}"
            share_url=(
                "https://t.me/share/url?url="
                + referral_link
                + "&text=Join%20this%20subscription%20bot"
            )

            text=(
                "🎁 Referral Program\n\n"
                f"👥 Total Referrals: {total}\n"
                f"✅ Successful Referrals: {successful}\n"
                f"🎉 Reward: {reward_days} Free Days per successful referral.\n\n"
                "🔗 Your Referral Link:\n"
                f"{referral_link}"
            )

            kb=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📤 Share Referral Link",
                    url=share_url,
                )],
                [InlineKeyboardButton(
                    "⬅ Back",
                    callback_data="c_home",
                )],
            ])

            await self.safe_query_message(q,text,kb)
            return

        if action=="c_support":
            support=await get_live_support_settings(owner)
            if not support.get("enabled"):
                await self.safe_query_message(
                    q,
                    "🔴 Live support is currently unavailable. Please try again later.",
                    back_keyboard,
                )
                return
            await self.safe_query_message(
                q,
                "💬 Live Support is ON.\n\nSend any text, photo, video, voice, audio, document or sticker here. Your message will stay in the support conversation and will not be auto-deleted.",
                back_keyboard,
            )
            return

        await q.answer(
            "Button action not found",
            show_alert=True,
        )

    async def admin_callback(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        q=update.callback_query; await q.answer(); owner=self.owner(context)
        staff = await self.staff_record(update, context)
        if not staff:
            await q.edit_message_text("❌ Not authorized")
            return
        a=q.data
        role = staff.get("role", "moderator")
        if role == "moderator":
            allowed_prefixes = ("a_home", "a_users", "a_user_", "a_pending", "a_pay_", "a_live_support", "a_help")
            if not any(a == p or a.startswith(p) for p in allowed_prefixes):
                await q.answer("Moderator permission is not available for this section.", show_alert=True)
                return
        if role != "seller" and a.startswith("a_staff"):
            await q.answer("Only the seller can manage staff.", show_alert=True)
            return
        if a=="a_home":
            context.user_data.clear()
            await q.edit_message_text(
                await self.admin_panel_text(owner, q.from_user),
                reply_markup=self.admin_menu(),
                parse_mode="HTML",
            )
            return
        if a=="a_seller_profile":
            plan,assignment=await effective_plan(owner)
            usage=await stats(owner)
            bot_record=await get_bot_by_data_owner_id(owner) or {}
            expiry=(assignment or {}).get("expiry_date")
            if expiry and getattr(expiry,"tzinfo",None) is None:
                expiry=expiry.replace(tzinfo=timezone.utc)
            now=datetime.now(timezone.utc)
            if expiry and expiry>now:
                remaining=expiry-now
                remaining_text=f"{remaining.days}d {remaining.seconds//3600}h {(remaining.seconds%3600)//60}m"
                status="✅ Active"
            elif str(plan.get("plan_id","free"))=="free":
                remaining_text="No expiry"
                status="🆓 Free Plan"
            else:
                remaining_text="Expired"
                status="❌ Expired"
            def lim(value):
                try:
                    value=int(value)
                    return "Unlimited" if value<0 else f"{value:,}"
                except Exception:
                    return str(value)
            text=(
                "👤 Seller Profile\n\n"
                f"🆔 Seller ID: {owner}\n"
                f"👤 Name: {q.from_user.full_name or 'Unknown'}\n"
                f"📝 Username: @{q.from_user.username}" if q.from_user.username else f"📝 Username: Not set"
            )
            text += (
                "\n\n💎 Plan Details\n"
                f"Plan: {plan.get('name','Free')}\n"
                f"Status: {status}\n"
                f"Expiry: {self.format_dt(expiry)}\n"
                f"Remaining: {remaining_text}\n\n"
                "📊 Usage & Limits\n"
                f"🤖 Clone Bots: {1 if bot_record else 0} / {lim(plan.get('bot_limit',1))}\n"
                f"👥 Active Subscribers: {usage.get('active',0)} / {lim(plan.get('active_subscriber_limit',25))}\n"
                f"📢 Channels / Groups: {usage.get('channels',0)} / {lim(plan.get('channel_limit',1))}\n"
                f"📦 Subscription Plans: {usage.get('plans',0)} / {lim(plan.get('plan_limit',2))}\n\n"
                f"👥 Total Users: {usage.get('users',0)}\n"
                f"💳 Pending Payments: {usage.get('pending',0)}\n"
                f"💰 Revenue: ₹{usage.get('revenue',0):g}"
            )
            kb=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Buy / Change Plan",callback_data="seller_upgrade_plan")],
                [InlineKeyboardButton("📜 Plan History",callback_data="a_seller_plan_history")],
                [InlineKeyboardButton("🤝 Seller Referral",callback_data="a_seller_referral")],
                [InlineKeyboardButton("🆘 Help & Commands",callback_data="a_help")],
                [InlineKeyboardButton("⬅ Seller Admin Panel",callback_data="a_home")],
            ])
            await q.edit_message_text(text,reply_markup=kb)
            return
        if a=="a_seller_plan_history":
            await q.edit_message_text(
                "📜 Seller Plan History\n\nOpen the main SaaS bot → Seller Dashboard → Plan History to view complete seller plan records.",
                reply_markup=self.back("a_seller_profile"),
            )
            return
        if a=="a_plans": await q.edit_message_text("📦 Plan Management",reply_markup=self.plans_admin_menu()); return
        if a=="a_plan_add":
            plan_cfg,_=await effective_plan(owner)
            existing=len(await get_plans(owner))
            limit=int(plan_cfg.get("plan_limit",2))
            if limit>=0 and existing>=limit:
                await q.edit_message_text(await plan_limit_warning(owner), reply_markup=self.limit_keyboard("a_plans")); return
            context.user_data.clear(); context.user_data["wait_plan_add"]=True; await q.edit_message_text("Send: Plan Name | Duration | Price\nExample: Premium | 30d | 199",reply_markup=self.back("a_plans")); return
        if a=="a_plan_list":
            plans=await get_plans(owner); lines=["📋 Plans\n"]; kb=[]
            for p in plans:
                lines.append(f"{'✅' if p.get('active') else '⏸'} {p['name']} — {p['duration_text']} — ₹{p['price']:g}")
                kb.append([InlineKeyboardButton(f"✏ {p['name'][:16]}",callback_data=f"a_plan_edit_{p['plan_id']}"),InlineKeyboardButton("🗑",callback_data=f"a_plan_del_{p['plan_id']}")])
                kb.append([InlineKeyboardButton("⏸ Disable" if p.get("active") else "▶ Enable",callback_data=f"a_plan_toggle_{p['plan_id']}")])
            kb.append([InlineKeyboardButton("⬅ Back",callback_data="a_plans")]); await q.edit_message_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(kb)); return
        if a.startswith("a_plan_edit_"): context.user_data.clear(); context.user_data["wait_plan_edit"]=a.replace("a_plan_edit_",""); await q.edit_message_text("Send new: Plan Name | Duration | Price",reply_markup=self.back("a_plan_list")); return
        if a.startswith("a_plan_del_"): await delete_plan(owner,a.replace("a_plan_del_","")); await q.edit_message_text("✅ Plan deleted",reply_markup=self.plans_admin_menu()); return
        if a.startswith("a_plan_toggle_"):
            pid=a.replace("a_plan_toggle_",""); p=await get_plan(owner,pid); await update_plan(owner,pid,active=not bool(p.get("active"))); await q.edit_message_text("✅ Plan status updated",reply_markup=self.plans_admin_menu()); return
        if a=="a_channels": await q.edit_message_text("📢 Channels / Groups",reply_markup=self.channels_menu()); return
        if a=="a_channel_add":
            plan_cfg,_=await effective_plan(owner)
            existing=len(await get_channels(owner))
            limit=int(plan_cfg.get("channel_limit",1))
            if limit>=0 and existing>=limit:
                await q.edit_message_text(await plan_limit_warning(owner), reply_markup=self.limit_keyboard("a_channels")); return
            context.user_data.clear(); context.user_data["wait_channel"]=True; await q.edit_message_text(
                "📢 Connect Channel / Group\n\n"
                "✅ Channel\n"
                "• Child bot ko channel me Admin banao.\n"
                "• Channel se koi bhi message yahan FORWARD karo.\n\n"
                "✅ Private Group (Recommended)\n"
                "1. Child bot ko group me add karo.\n"
                "2. Bot ko Admin banao.\n"
                "3. Invite Users permission ON rakho.\n"
                "4. Usi group ke andar /connectgroup bhejo.\n\n"
                "Bot group automatically detect karke save karega aur invite-link permission test karega.\n\n"
                "🔄 Agar auto detect na ho:\n"
                "• Group se koi message yahan FORWARD karo.\n\n"
                "⚠️ Sirf last option:\n"
                "-100xxxxxxxxxx | Group Name",
                reply_markup=self.back("a_channels"),
            ); return
        if a=="a_channel_list":
            channels=await get_channels(owner); lines=["📋 Channels / Groups\n"]; kb=[]
            for ch in channels:
                lines.append(f"• {ch.get('title')}\n  {ch.get('chat_id')}")
                kb.append([InlineKeyboardButton(f"❌ {ch.get('title','Chat')[:18]}",callback_data=f"a_channel_del_{ch['chat_id']}")])
            kb.append([InlineKeyboardButton("⬅ Back",callback_data="a_channels")]); await q.edit_message_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup(kb)); return
        if a=="a_channel_resend":
            channels=await get_channels(owner)
            if not channels:
                await q.edit_message_text(
                    "❌ Pehle kam se kam ek channel/group add karo.",
                    reply_markup=self.channels_menu(),
                )
                return
            active_count=len(await active_subscriptions(owner))
            await q.edit_message_text(
                "🔗 Group/Channel Invite Link Resend\n\n"
                f"Active subscribers found: {active_count}\n"
                f"Channels/Groups: {len(channels)}\n\n"
                "Fresh invite links sabhi active subscribers ko bheje jayenge. "
                "Expired users ko message nahi jayega.\n\nContinue?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, Resend",callback_data="a_channel_resend_yes")],
                    [InlineKeyboardButton("❌ No",callback_data="a_channels")],
                ]),
            )
            return
        if a=="a_channel_resend_yes":
            await q.edit_message_text("⏳ Invite links resend ho rahe hain...")
            channels=await get_channels(owner)
            subscriptions=await active_subscriptions(owner)
            sent=failed=invite_failed=0
            now=datetime.now(timezone.utc)

            for sub in subscriptions:
                user_id=int(sub["user_id"])
                expiry=sub.get("expiry_date")
                if expiry and expiry.tzinfo is None:
                    expiry=expiry.replace(tzinfo=timezone.utc)
                remaining=expiry-now if expiry else None
                if not remaining or remaining.total_seconds()<=0:
                    continue
                days=remaining.days
                hours=remaining.seconds//3600
                minutes=(remaining.seconds%3600)//60
                link_lines=[]
                for ch in channels:
                    try:
                        invite=await context.bot.create_chat_invite_link(
                            chat_id=ch["chat_id"],
                            member_limit=1,
                        )
                        await save_invite(owner, user_id, ch["chat_id"], invite.invite_link)
                        link_lines.append(
                            f"📢 {ch.get('title','Premium Channel')}\n{invite.invite_link}"
                        )
                    except Exception as exc:
                        invite_failed+=1
                        logger.warning(
                            "Invite create failed owner=%s chat=%s user=%s: %s",
                            owner,ch.get("chat_id"),user_id,exc,
                        )

                if not link_lines:
                    failed+=1
                    continue

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "📢 Channel/Group Invite Links Updated\n\n"
                            "Your subscription is still active.\n\n"
                            f"⏱ Remaining: {days}d {hours}h {minutes}m\n\n"
                            "Join using the fresh invite link(s):\n\n"
                            + "\n\n".join(link_lines)
                        ),
                        disable_web_page_preview=True,
                    )
                    sent+=1
                except Exception as exc:
                    failed+=1
                    await save_failed_delivery(owner,user_id,"invite_resend",{"channels":[c.get("chat_id") for c in channels]},str(exc))
                    logger.warning(
                        "Invite resend failed owner=%s user=%s: %s",
                        owner,user_id,exc,
                    )
                await asyncio.sleep(0.05)

            await q.edit_message_text(
                "✅ Invite Link Resend Completed\n\n"
                f"Active subscribers: {len(subscriptions)}\n"
                f"Successfully sent: {sent}\n"
                f"Failed/blocked users: {failed}\n"
                f"Invite creation failures: {invite_failed}\n\n"
                "Expired users ko message nahi bheja gaya.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Retry Failed Users", callback_data="a_retry_failed")],
                    [InlineKeyboardButton("⬅ Back", callback_data="a_channels")],
                ]),
            )
            return
        if a == "a_retry_failed":
            failed_docs = await get_failed_deliveries(
                owner,
                "invite_resend",
            )
            sent = still_failed = skipped = 0
            channels = await get_channels(owner)

            for item in failed_docs:
                claimed = await claim_failed_delivery(
                    item["_id"],
                    owner,
                    stale_after_seconds=600,
                )
                if not claimed:
                    skipped += 1
                    continue

                uid = int(claimed.get("user_id"))

                try:
                    links = []

                    for ch in channels:
                        invite = await context.bot.create_chat_invite_link(
                            ch["chat_id"],
                            member_limit=1,
                        )
                        await save_invite(
                            owner,
                            uid,
                            ch["chat_id"],
                            invite.invite_link,
                        )
                        links.append(
                            f"{ch.get('title', 'Channel')}: "
                            f"{invite.invite_link}"
                        )

                    await context.bot.send_message(
                        uid,
                        "🔁 Fresh invite link(s):\n\n"
                        + "\n".join(links),
                        disable_web_page_preview=True,
                    )

                    resolved = await resolve_failed_delivery(
                        claimed["_id"]
                    )
                    if resolved:
                        sent += 1
                    else:
                        still_failed += 1
                        logger.warning(
                            "Failed delivery retry sent but could not "
                            "finalize owner_id=%s user_id=%s delivery_id=%s",
                            owner,
                            uid,
                            claimed["_id"],
                        )
                except Exception as exc:
                    still_failed += 1
                    logger.exception(
                        "Failed delivery retry failed owner_id=%s "
                        "user_id=%s delivery_id=%s",
                        owner,
                        uid,
                        claimed["_id"],
                    )
                    try:
                        await release_failed_delivery_claim(
                            claimed["_id"],
                            str(exc),
                        )
                    except Exception:
                        logger.exception(
                            "Failed delivery claim release failed "
                            "owner_id=%s user_id=%s delivery_id=%s",
                            owner,
                            uid,
                            claimed["_id"],
                        )

            await q.edit_message_text(
                "🔁 Retry completed\n\n"
                f"Sent: {sent}\n"
                f"Still failed: {still_failed}\n"
                f"Already processing: {skipped}",
                reply_markup=self.admin_menu(),
            )
            return
        if a.startswith("a_channel_del_"): await remove_channel(owner,int(a.replace("a_channel_del_",""))); await q.edit_message_text("✅ Removed",reply_markup=self.channels_menu()); return
        if a=="a_welcome":
            s=await ensure_seller_defaults(owner,(await get_bot_by_data_owner_id(owner) or {}).get("bot_name","Subscription Bot"))
            text=("💬 Welcome Message\n\n"
                  f"📝 Text: {'✅' if s.get('welcome_message') else '❌'}\n"
                  f"🖼 Media: {'✅' if s.get('welcome_media_file_id') else '❌'}\n"
                  f"🔗 Buttons: {sum(len(r) for r in (s.get('welcome_buttons') or []))}")
            await q.edit_message_text(text,reply_markup=self.welcome_menu()); return
        if a=="a_welcome_text":
            context.user_data.clear(); context.user_data["wait_welcome_text"]=True
            await q.edit_message_text("📝 Send welcome text.\n\nHTML is supported.\nVariables: {ID} {NAME} {SURNAME} {NAMESURNAME} {USERNAME} {LANG} {DATE} {TIME} {WEEKDAY} {MENTION} {BOTNAME}",reply_markup=self.back("a_welcome")); return
        if a=="a_welcome_media":
            context.user_data.clear(); context.user_data["wait_welcome_media"]=True
            await q.edit_message_text("🖼 Send photo, video, GIF or document for welcome media.\n\nThe same media will appear in Preview and on /start.",reply_markup=self.back("a_welcome")); return
        if a=="a_welcome_buttons": await q.edit_message_text("🔗 Welcome Buttons",reply_markup=self.welcome_buttons_menu()); return
        if a=="a_welcome_quick": await q.edit_message_text("⚡ Choose a bot button to add",reply_markup=self.welcome_quick_menu()); return
        if a.startswith("a_wq_"):
            feature=a.replace("a_wq_","")
            config={
                "plans":("📋 Plans","c_plans"),"buy":("💳 Buy","c_buy"),"profile":("👤 My Profile","c_profile"),
                "renew":("🔄 Renew","c_renew"),"referral":("🎁 Referral","c_referral"),"support":("📞 Support","c_support"),"home":("🏠 Main Menu","c_home")}
            title,callback=config[feature]
            s=await get_seller_settings(owner)
            rows=s.get("welcome_buttons") or []

            already_exists=any(
                item.get("type")=="callback"
                and item.get("value")==callback
                for row in rows
                for item in row
            )

            if already_exists:
                await q.edit_message_text(
                    f"ℹ️ {title} button already exists.",
                    reply_markup=self.welcome_buttons_menu(),
                )
                return

            rows.append([
                {
                    "text":title,
                    "type":"callback",
                    "value":callback,
                }
            ])

            await set_seller_setting(
                owner,
                "welcome_buttons",
                rows,
            )

            await q.edit_message_text(
                f"✅ {title} button added.",
                reply_markup=self.welcome_buttons_menu(),
            )
            return
        if a=="a_welcome_manual":
            context.user_data.clear(); context.user_data["wait_welcome_buttons"]=True
            await q.edit_message_text("✍ Send buttons in this format:\n\nSingle button:\nJoin Channel - https://t.me/example\n\nSame row:\nPlans - feature:plans && Buy - feature:buy\n\nNew line = new row.\nFeatures: plans, buy, profile, renew, referral, support, home",reply_markup=self.back("a_welcome_buttons")); return
        if a=="a_welcome_see_buttons":
            s=await get_seller_settings(owner)
            rows=s.get("welcome_buttons") or []

            if not rows:
                await q.edit_message_text(
                    "No buttons set.",
                    reply_markup=self.welcome_buttons_menu(),
                )
                return

            lines=["🔗 Current Buttons\n"]
            kb=[]

            for row_index,row in enumerate(rows):
                names=[]

                for button_index,item in enumerate(row):
                    name=item.get("text","Button")
                    names.append(name)
                    kb.append([
                        InlineKeyboardButton(
                            f"🗑 Delete: {name[:28]}",
                            callback_data=(
                                f"a_welcome_delbtn_"
                                f"{row_index}_{button_index}"
                            ),
                        )
                    ])

                lines.append(
                    f"Row {row_index + 1}: "
                    + " | ".join(names)
                )

            kb.append([
                InlineKeyboardButton(
                    "➕ Add More",
                    callback_data="a_welcome_buttons",
                )
            ])
            kb.append([
                InlineKeyboardButton(
                    "⬅ Back",
                    callback_data="a_welcome_buttons",
                )
            ])

            await q.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return

        if a.startswith("a_welcome_delbtn_"):
            try:
                position=a.replace(
                    "a_welcome_delbtn_",
                    "",
                )
                row_index,button_index=[
                    int(value)
                    for value in position.split("_",1)
                ]

                s=await get_seller_settings(owner)
                rows=s.get("welcome_buttons") or []

                if row_index>=len(rows) or button_index>=len(rows[row_index]):
                    raise IndexError

                deleted_name=rows[row_index][button_index].get(
                    "text",
                    "Button",
                )

                del rows[row_index][button_index]

                if not rows[row_index]:
                    del rows[row_index]

                await set_seller_setting(
                    owner,
                    "welcome_buttons",
                    rows,
                )

                await q.edit_message_text(
                    f"✅ {deleted_name} button deleted.",
                    reply_markup=self.welcome_buttons_menu(),
                )
            except (ValueError,IndexError):
                await q.edit_message_text(
                    "❌ Button not found. Open Current Buttons again.",
                    reply_markup=self.welcome_buttons_menu(),
                )
            return
        if a=="a_welcome_remove_text": await set_seller_setting(owner,"welcome_message",""); await q.edit_message_text("✅ Welcome text removed.",reply_markup=self.welcome_menu()); return
        if a=="a_welcome_remove_media":
            await set_seller_setting(owner,"welcome_media_type",""); await set_seller_setting(owner,"welcome_media_file_id","")
            await q.edit_message_text("✅ Welcome media removed.",reply_markup=self.welcome_menu()); return
        if a=="a_welcome_remove_buttons": await set_seller_setting(owner,"welcome_buttons",[]); await q.edit_message_text("✅ Welcome buttons removed.",reply_markup=self.welcome_menu()); return
        if a=="a_welcome_preview":
            s=await ensure_seller_defaults(owner,(await get_bot_by_data_owner_id(owner) or {}).get("bot_name","Subscription Bot"))
            try:
                await q.message.reply_text("👀 Preview — users will see the message below:")
                await self.send_welcome(q.message,context,s,q.from_user)
            except Exception as exc:
                logger.exception("Welcome preview failed for owner=%s",owner)
                await q.message.reply_text(f"❌ Preview failed: {str(exc)[:300]}",reply_markup=self.welcome_menu())
            return
        if a=="a_pg_home":
            cfg=await get_gateway_config("seller",owner,decrypt=True)
            rows=[]; lines=["🌐 Automatic Payment Gateways","",f"Default: {cfg.get('default_gateway','manual').title()}",f"Manual: {'ON' if cfg.get('manual_enabled',True) else 'OFF'}",""]
            for gateway in SUPPORTED_GATEWAYS:
                g=(cfg.get("gateways") or {}).get(gateway,{})
                lines.append(f"{'✅' if g.get('enabled') else '❌'} {gateway.title()} ({g.get('mode','test')})")
                rows.append([InlineKeyboardButton(gateway.title(),callback_data=f"a_pg_view_{gateway}")])
            rows += [[InlineKeyboardButton("⚙ Default / Manual",callback_data="a_pg_default")],[InlineKeyboardButton("📜 History",callback_data="a_pg_history")],[InlineKeyboardButton("⬅ Back",callback_data="a_payment")]]
            await q.edit_message_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(rows)); return
        if a.startswith("a_pg_view_"):
            gateway=a.replace("a_pg_view_",""); cfg=await get_gateway_config("seller",owner,decrypt=True); g=(cfg.get("gateways") or {}).get(gateway,{})
            await q.edit_message_text(
                f"💳 {gateway.title()}\n\n"
                f"Status: {'Enabled' if g.get('enabled') else 'Disabled'}\n"
                f"Mode: {g.get('mode','test').title()}\n"
                f"Credentials: {'Set' if len(g)>2 else 'Not set'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Enable/Disable",callback_data=f"a_pg_toggle_{gateway}")],
                    [InlineKeyboardButton("🔑 Set Credentials",callback_data=f"a_pg_creds_{gateway}")],
                    [InlineKeyboardButton("✅ Test Connection",callback_data=f"a_pg_testconn_{gateway}")],
                    [InlineKeyboardButton("🧪 Test Mode",callback_data=f"a_pg_mode_test_{gateway}"),InlineKeyboardButton("🚀 Live Mode",callback_data=f"a_pg_mode_live_{gateway}")],
                    [InlineKeyboardButton("⬅ Back",callback_data="a_pg_home")],
                ]),
            ); return
        if a.startswith("a_pg_toggle_"):
            gateway=a.replace("a_pg_toggle_",""); cfg=await get_gateway_config("seller",owner,decrypt=True); g=(cfg.get("gateways") or {}).get(gateway,{})
            await save_gateway_config("seller",owner,gateway,{"enabled":not bool(g.get("enabled"))}); await q.edit_message_text("✅ Gateway status updated",reply_markup=self.payment_menu()); return
        if a.startswith("a_pg_testconn_"):
            gateway=a.replace("a_pg_testconn_","")
            try:
                result=await test_gateway_connection("seller",owner,gateway)
                await q.edit_message_text(
                    f"✅ {gateway.title()} connection successful.\n\nMode: {result.get('mode','test').title()}\nAPI access verified.",
                    reply_markup=self.back(f"a_pg_view_{gateway}"),
                )
            except GatewayError as exc:
                await q.edit_message_text(
                    f"❌ {gateway.title()} connection failed.\n\n{exc}",
                    reply_markup=self.back(f"a_pg_view_{gateway}"),
                )
            return
        if a.startswith("a_pg_mode_test_") or a.startswith("a_pg_mode_live_"):
            mode="test" if a.startswith("a_pg_mode_test_") else "live"; gateway=a.rsplit("_",1)[-1]
            await save_gateway_config("seller",owner,gateway,{"mode":mode}); await q.edit_message_text(f"✅ {gateway.title()} mode: {mode}",reply_markup=self.payment_menu()); return
        if a.startswith("a_pg_creds_"):
            gateway=a.replace("a_pg_creds_",""); context.user_data.clear(); context.user_data["wait_pg_credentials"]=gateway
            help_text={"razorpay":"KEY_ID | KEY_SECRET | WEBHOOK_SECRET","cashfree":"CLIENT_ID | CLIENT_SECRET","phonepe":"CLIENT_ID | CLIENT_VERSION | CLIENT_SECRET | WEBHOOK_USERNAME | WEBHOOK_PASSWORD","paytm":"MID | MERCHANT_KEY | WEBSITE_NAME"}[gateway]
            await q.edit_message_text(f"Send credentials:\n{help_text}",reply_markup=self.back("a_pg_home")); return
        if a=="a_pg_default":
            cfg=await get_gateway_config("seller",owner,decrypt=True); rows=[[InlineKeyboardButton("Manual Screenshot",callback_data="a_pg_setdefault_manual")]]
            for gateway in SUPPORTED_GATEWAYS: rows.append([InlineKeyboardButton(gateway.title(),callback_data=f"a_pg_setdefault_{gateway}")])
            rows += [[InlineKeyboardButton("🔄 Manual On/Off",callback_data="a_pg_manualtoggle")],[InlineKeyboardButton("⬅ Back",callback_data="a_pg_home")]]
            await q.edit_message_text("Choose default payment method",reply_markup=InlineKeyboardMarkup(rows)); return
        if a=="a_pg_manualtoggle":
            cfg=await get_gateway_config("seller",owner,decrypt=True); await set_gateway_preferences("seller",owner,manual_enabled=not cfg.get("manual_enabled",True)); await q.edit_message_text("✅ Manual payment updated",reply_markup=self.payment_menu()); return
        if a.startswith("a_pg_setdefault_"):
            gateway=a.replace("a_pg_setdefault_",""); await set_gateway_preferences("seller",owner,default_gateway=gateway); await q.edit_message_text(f"✅ Default: {gateway.title()}",reply_markup=self.payment_menu()); return
        if a=="a_pg_history":
            items=await gateway_history("seller",owner,25); text="📜 Gateway History\n\n"+"\n".join(f"• {x.get('gateway','-').title()} ₹{x.get('amount',0):g} — {x.get('status')}" for x in items)
            await q.edit_message_text(text if items else "No gateway payments yet",reply_markup=self.back("a_pg_home")); return

        if a=="a_live_support":
            support=await get_live_support_settings(owner)
            blocked=await count_support_blocks(owner)
            await q.edit_message_text(
                self.live_support_text(support,blocked),
                reply_markup=self.live_support_menu(support),
            ); return
        if a=="a_live_support_toggle":
            support=await get_live_support_settings(owner)
            updated=await update_live_support_settings(owner,enabled=not bool(support.get("enabled")))
            blocked=await count_support_blocks(owner)
            await q.edit_message_text(
                self.live_support_text(updated,blocked),
                reply_markup=self.live_support_menu(updated),
            ); return
        if a in {"a_live_support_mode_private","a_live_support_mode_topic"}:
            mode="private" if a.endswith("private") else "topic"
            updated=await update_live_support_settings(owner,mode=mode)
            blocked=await count_support_blocks(owner)
            await q.edit_message_text(
                self.live_support_text(updated,blocked),
                reply_markup=self.live_support_menu(updated),
            ); return
        if a=="a_live_support_group_info":
            support=await get_live_support_settings(owner)
            await q.edit_message_text(
                "📌 Support Group\n\n"
                f"Name: {support.get('support_group_title') or 'Not connected'}\n"
                f"Chat ID: {support.get('support_group_id') or '-'}\n\n"
                "Group badalne ke liye naye forum group me /connectsupport bhejo.",
                reply_markup=self.back("a_live_support"),
            ); return
        if a=="a_live_support_blocks":
            blocked=await count_support_blocks(owner)
            await q.edit_message_text(
                f"🚫 Support-blocked users: {blocked}\n\n"
                "User ke support topic ke first details message se Block/Unblock kiya ja sakta hai.",
                reply_markup=self.back("a_live_support"),
            ); return
        if a=="a_support_templates":
            templates=await list_support_templates(owner)
            text="⚡ Live Support Reply Templates\n\nTopic/private support me saved command bhejo, jaise /payment. Bot saved text, media aur buttons user ko reply ke roop me bhejega.\n\nVariables: {NAME} {ID} {USERNAME} {PLAN} {EXPIRY}"
            await q.edit_message_text(text,reply_markup=self.support_templates_menu(templates)); return
        if a=="a_support_tpl_add":
            context.user_data.clear(); context.user_data["wait_support_tpl_command"]=True
            await q.edit_message_text("Command name bhejo. Example: payment\n\nSlash mat lagao. Sirf letters, numbers aur underscore.",reply_markup=self.back("a_support_templates")); return
        if a.startswith("a_support_tpl_view_"):
            command=a.replace("a_support_tpl_view_","")
            tpl=await get_support_template(owner,command)
            if not tpl:
                await q.edit_message_text("❌ Template not found",reply_markup=self.back("a_support_templates")); return
            count=sum(len(row) for row in (tpl.get("buttons") or []))
            auto_delete=_format_auto_delete(_template_auto_delete_seconds(tpl))
            await q.edit_message_text(f"⚡ /{command}\n\n📝 Text: {'✅' if tpl.get('text') else '❌'}\n🖼 Media: {'✅' if tpl.get('media_file_id') else '❌'}\n🔗 Buttons: {count}\n⏱ Auto Remove: {auto_delete}",reply_markup=self.support_template_edit_menu(command)); return
        if a.startswith("a_support_tpl_text_"):
            command=a.replace("a_support_tpl_text_",""); context.user_data.clear(); context.user_data["wait_support_tpl_text"]=command
            await q.edit_message_text("📝 Template text bhejo.\n\nVariables: {NAME} {ID} {USERNAME} {PLAN} {EXPIRY}",reply_markup=self.back(f"a_support_tpl_view_{command}")); return
        if a.startswith("a_support_tpl_media_"):
            command=a.replace("a_support_tpl_media_",""); context.user_data.clear(); context.user_data["wait_support_tpl_media"]=command
            await q.edit_message_text("🖼 Photo, video, GIF ya document bhejo.",reply_markup=self.back(f"a_support_tpl_view_{command}")); return
        if a.startswith("a_support_tpl_buttons_"):
            command=a.replace("a_support_tpl_buttons_",""); context.user_data.clear(); context.user_data["wait_support_tpl_buttons"]=command
            await q.edit_message_text("🔗 Buttons bhejo. Format:\nTitle - https://example.com\n\nSame row:\nButton 1 - URL && Button 2 - URL",reply_markup=self.back(f"a_support_tpl_view_{command}")); return
        if a.startswith("a_support_tpl_autodel_"):
            command=a.replace("a_support_tpl_autodel_","")
            tpl=await get_support_template(owner,command)
            if not tpl:
                await q.edit_message_text("❌ Template not found",reply_markup=self.back("a_support_templates")); return
            current=_template_auto_delete_seconds(tpl)
            await q.edit_message_text(
                f"⏱ Template Auto Remove — /{command}\n\nCurrent: {_format_auto_delete(current)}\n\nBot ka template reply selected time ke baad automatically remove hoga.",
                reply_markup=self.support_template_auto_delete_menu(command,current),
            ); return
        if a.startswith("a_tpl_ad_custom_"):
            command=a.replace("a_tpl_ad_custom_","")
            context.user_data.clear(); context.user_data["wait_support_tpl_auto_delete"]=command
            await q.edit_message_text(
                "⌨️ Custom auto-remove duration bhejo.\n\nExamples:\n30s = 30 seconds\n2m = 2 minutes\n1h = 1 hour\n6h = 6 hours\n1d = 1 day\noff = disable\n\nMaximum: 7 days",
                reply_markup=self.back(f"a_support_tpl_autodel_{command}"),
            ); return
        if a.startswith("a_tpl_ad_"):
            payload=a.replace("a_tpl_ad_", "", 1)
            seconds_text, command=payload.split("_",1)
            seconds=int(seconds_text)
            await save_support_template(owner,command,auto_delete_seconds=seconds)
            await q.edit_message_text(
                f"✅ Template Auto Remove updated\n\n/{command}: {_format_auto_delete(seconds)}",
                reply_markup=self.support_template_auto_delete_menu(command,seconds),
            ); return
        if a.startswith("a_support_tpl_rmtext_"):
            command=a.replace("a_support_tpl_rmtext_",""); await save_support_template(owner,command,text="")
            await q.edit_message_text("✅ Text removed",reply_markup=self.support_template_edit_menu(command)); return
        if a.startswith("a_support_tpl_rmmedia_"):
            command=a.replace("a_support_tpl_rmmedia_",""); await save_support_template(owner,command,media_type="",media_file_id="")
            await q.edit_message_text("✅ Media removed",reply_markup=self.support_template_edit_menu(command)); return
        if a.startswith("a_support_tpl_rmbuttons_"):
            command=a.replace("a_support_tpl_rmbuttons_",""); await save_support_template(owner,command,buttons=[])
            await q.edit_message_text("✅ Buttons removed",reply_markup=self.support_template_edit_menu(command)); return
        if a.startswith("a_support_tpl_delete_"):
            command=a.replace("a_support_tpl_delete_",""); await delete_support_template(owner,command)
            await q.edit_message_text(f"✅ /{command} deleted",reply_markup=self.support_templates_menu(await list_support_templates(owner))); return
        if a.startswith("a_support_tpl_preview_"):
            command=a.replace("a_support_tpl_preview_",""); tpl=await get_support_template(owner,command)
            await self.send_support_template(context,owner,q.from_user.id,tpl,q.from_user)
            await q.answer("Preview sent",show_alert=True); return

        if a.startswith("a_tz_"):
            key = a.replace("a_tz_", "", 1)
            if key == "manual":
                context.user_data.clear()
                context.user_data["wait_timezone"] = True
                settings = await get_seller_settings(owner)
                await q.edit_message_text(
                    timezone_guide(settings.get("timezone") or "Asia/Kolkata")
                    + "\n\nSend the timezone name now.",
                    reply_markup=self.back("a_settings"),
                )
                return
            timezone_name = timezone_from_key(key)
            if not timezone_name:
                await q.answer("Invalid timezone selection.", show_alert=True)
                return
            await set_seller_setting(owner, "timezone", timezone_name)
            context.user_data.clear()
            await q.edit_message_text(
                f"✅ Timezone updated!\n\nTimezone: {timezone_name}",
                reply_markup=self.settings_menu(),
            )
            return

        if a=="a_payment":
            s=await get_seller_settings(owner); await q.edit_message_text(f"💳 Payment Settings\n\nUPI Name: {s.get('upi_name') or 'Not Set'}\nUPI ID: {s.get('upi_id') or 'Not Set'}\nQR: {'Added' if s.get('upi_qr_file_id') else 'Not Added'}",reply_markup=self.payment_menu()); return
        state={"a_set_upi_id":("wait_upi_id","Send UPI ID","a_payment"),"a_set_upi_name":("wait_upi_name","Send UPI Name","a_payment"),"a_set_bot_name":("wait_bot_name","Send Bot Name","a_settings"),"a_set_support":("wait_support","Send Support Username","a_settings"),"a_set_currency":("wait_currency","Send Currency","a_settings"),"a_set_timezone":("wait_timezone","__TIMEZONE_PICKER__","a_settings"),"a_set_reminder":("wait_reminder","Send Reminder Days","a_settings"),"a_set_referral_days":("wait_referral_days","Send free reward days per successful referral","a_settings")}
        if a in state:
            key,msg,back=state[a]
            context.user_data.clear()
            if a == "a_set_timezone":
                settings = await get_seller_settings(owner)
                await q.edit_message_text(
                    timezone_guide(settings.get("timezone") or "Asia/Kolkata"),
                    reply_markup=timezone_keyboard("a_tz_", "a_settings"),
                )
            else:
                context.user_data[key]=True
                await q.edit_message_text(msg,reply_markup=self.back(back))
            return
        if a=="a_set_qr": context.user_data.clear(); context.user_data["wait_qr"]=True; await q.edit_message_text("Send QR image",reply_markup=self.back("a_payment")); return
        if a=="a_settings":
            s=await get_seller_settings(owner); await q.edit_message_text(f"⚙ Bot Settings\n\nBot Name: {s.get('bot_name')}\nSupport: {s.get('support_username') or 'Not Set'}\nCurrency: {s.get('currency')}\nTimezone: {s.get('timezone')}\nReminder: {s.get('reminder_days')}",reply_markup=self.settings_menu()); return
        if a=="a_pending":
            ps=await pending_payments(owner); lines=["📨 Pending Payments\n"]; kb=[]
            for p in ps:
                lines.append(f"• {p['user_id']} | ₹{p['amount']:g} | {p['plan']}")
                kb.append([InlineKeyboardButton(f"View {p['user_id']}",callback_data=f"a_pay_view_{p['payment_id']}")])
            kb.append([InlineKeyboardButton("⬅ Back",callback_data="a_home")]); await q.edit_message_text("\n".join(lines) if ps else "📨 No pending payments",reply_markup=InlineKeyboardMarkup(kb)); return
        if a.startswith("a_pay_view_"):
            p=await get_payment(owner,a.replace("a_pay_view_",""));
            if not p: await q.edit_message_text("Not found",reply_markup=self.admin_menu()); return
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve",callback_data=f"a_pay_ok_{p['payment_id']}"),InlineKeyboardButton("❌ Reject",callback_data=f"a_pay_no_{p['payment_id']}")],[InlineKeyboardButton("⬅ Back",callback_data="a_pending")]])
            caption=await self.payment_details_caption(
                owner,
                p,
                status=p.get("status","pending"),
            )
            await q.message.reply_photo(
                p["screenshot_file_id"],
                caption=caption,
                reply_markup=kb,
            )
            return
        if a.startswith("a_pay_ok_") or a.startswith("a_pay_no_"):
            approve=a.startswith("a_pay_ok_")
            pid=a.replace(
                "a_pay_ok_" if approve else "a_pay_no_",
                "",
                1,
            )
            p=await get_payment(owner,pid)

            if not p:
                await q.answer(
                    "Payment not found",
                    show_alert=True,
                )
                return

            current_status=p.get("status","pending")

            if current_status in {"approved","rejected"}:
                final_caption=await self.payment_details_caption(
                    owner,
                    p,
                    status=current_status,
                    processed_by=p.get("admin_id"),
                )
                try:
                    await q.edit_message_caption(
                        caption=final_caption,
                        reply_markup=None,
                    )
                except BadRequest:
                    pass
                await q.answer(
                    f"Already {current_status}",
                    show_alert=True,
                )
                return

            if not approve:
                changed=await set_payment_status(
                    owner,
                    pid,
                    "rejected",
                    owner,
                )
                if not changed:
                    await q.answer(
                        "Payment is already being processed",
                        show_alert=True,
                    )
                    return

                await context.bot.send_message(
                    p["user_id"],
                    "❌ Payment rejected",
                )
                rejected_caption=await self.payment_details_caption(
                    owner,
                    p,
                    status="rejected",
                    processed_by=owner,
                )
                await q.edit_message_caption(
                    caption=rejected_caption,
                    reply_markup=None,
                )
                return

            claimed=await claim_payment_for_processing(
                owner,
                pid,
                owner,
            )
            if not claimed:
                latest=await get_payment(owner,pid)
                latest_status=(latest or {}).get("status","unknown")
                await q.answer(
                    f"Payment status: {latest_status}",
                    show_alert=True,
                )
                return

            try:
                plan_cfg,_=await effective_plan(owner)
                active_now=await active_subscriptions(owner)
                already_active=any(int(x.get("user_id"))==int(p["user_id"]) for x in active_now)
                sub_limit=int(plan_cfg.get("active_subscriber_limit",25))
                if not already_active and sub_limit>=0 and len(active_now)>=sub_limit:
                    await release_processing_payment(owner,pid,"seller subscriber limit reached")
                    await q.answer("Seller plan limit reached",show_alert=True)
                    await context.bot.send_message(owner, await plan_limit_warning(owner), reply_markup=self.limit_keyboard("a_pending"))
                    return
                expiry=await activate_subscription(
                    owner,
                    p["user_id"],
                    p["plan"],
                    p["duration_minutes"],
                    amount=p.get("amount"),
                    duration_text=p.get("duration_text"),
                )

                referral=await mark_referral_rewarded(
                    owner,
                    p["user_id"],
                    payment_id=pid,
                )
                if referral:
                    settings=await get_seller_settings(owner)
                    reward_days=int(
                        settings.get("referral_reward_days",7) or 0
                    )
                    referrer_id=int(referral["referrer_user_id"])

                    try:
                        if reward_days>0:
                            await activate_subscription(
                                owner,
                                referrer_id,
                                "Referral Reward",
                                reward_days*1440,
                                amount=0,
                                duration_text=f"{reward_days}d",
                            )

                        finalized_reward=await finalize_referral_reward(
                            owner,
                            p["user_id"],
                            payment_id=pid,
                        )
                        if not finalized_reward:
                            raise RuntimeError(
                                "Referral reward finalization was not applied"
                            )

                        if reward_days>0:
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    "🎉 Referral Reward Added!\n"
                                    f"You received {reward_days} free day(s).",
                                )
                            except Exception:
                                logger.exception(
                                    "Referral reward notification failed "
                                    "owner=%s referrer=%s payment=%s",
                                    owner,
                                    referrer_id,
                                    pid,
                                )
                    except Exception as exc:
                        await release_referral_reward(
                            owner,
                            p["user_id"],
                            str(exc),
                            payment_id=pid,
                        )
                        logger.exception(
                            "Referral reward processing failed "
                            "owner=%s referred=%s payment=%s",
                            owner,
                            p["user_id"],
                            pid,
                        )

                links=[]
                for ch in await get_channels(owner):
                    try:
                        inv=await context.bot.create_chat_invite_link(
                            ch["chat_id"],
                            member_limit=1,
                        )
                        await save_invite(owner, p["user_id"], ch["chat_id"], inv.invite_link)
                        links.append(
                            f"{ch.get('title')}: {inv.invite_link}"
                        )
                    except Exception as exc:
                        links.append(
                            f"{ch.get('title')}: invite failed ({exc})"
                        )

                finalized=await finalize_processed_payment(
                    owner,
                    pid,
                    "approved",
                    owner,
                )
                if not finalized:
                    raise RuntimeError(
                        "Could not finalize payment status"
                    )

                expiry_text=self.format_dt(expiry)
                invoice=await create_invoice(owner,p["user_id"],p,(await get_seller_settings(owner)).get("bot_name","Seller"))
                await audit("child_payment_approved",owner,owner,{"payment_id":pid,"invoice_no":invoice["invoice_no"]})
                await context.bot.send_message(
                    p["user_id"],
                    "🎉 Payment approved\n"
                    f"Plan: {p['plan']}\n"
                    f"Added validity: {p.get('duration_text') or '-'}\n"
                    f"New expiry: {expiry_text}\n"
                    f"Receipt/Invoice: {invoice['invoice_no']}\n\n"
                    + "\n".join(links),
                )

                approved_caption=await self.payment_details_caption(
                    owner,
                    p,
                    status="approved",
                    processed_by=owner,
                )
                approved_caption+=(
                    "\n"
                    f"📅 New Expiry: {expiry_text}\n"
                    "➕ Remaining validity was preserved and "
                    "the new plan duration was added."
                )

                await q.edit_message_caption(
                    caption=approved_caption,
                    reply_markup=None,
                )

            except Exception as exc:
                logger.exception(
                    "Payment approval failed owner=%s payment=%s",
                    owner,
                    pid,
                )
                await release_processing_payment(
                    owner,
                    pid,
                    str(exc),
                )
                await q.answer(
                    "Approval failed. Payment is still pending; "
                    "you can press Approve again.",
                    show_alert=True,
                )
                try:
                    await q.edit_message_caption(
                        caption=(
                            await self.payment_details_caption(
                                owner,
                                p,
                                status="pending",
                            )
                            + "\n\n⚠️ Last approval attempt failed. "
                            "Payment was kept pending safely."
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    "✅ Approve",
                                    callback_data=f"a_pay_ok_{pid}",
                                ),
                                InlineKeyboardButton(
                                    "❌ Reject",
                                    callback_data=f"a_pay_no_{pid}",
                                ),
                            ]
                        ]),
                    )
                except Exception:
                    pass
            return

        if a=="a_history":
            ps=await payment_history(owner); text="📜 Payment History\n\n"+"\n".join(f"{'✅' if p['status']=='approved' else '❌'} {p['user_id']} ₹{p['amount']:g} {p['plan']}" for p in ps[:20]); await q.edit_message_text(text,reply_markup=self.back()); return
        if a=="a_broadcast_schedule":
            context.user_data.clear(); context.user_data["wait_scheduled_broadcast"]=True
            await q.edit_message_text("🗓 Send a message with first line in this format:\nYYYY-MM-DD HH:MM\n\nWrite the broadcast text after the first line. Time uses your configured timezone.",reply_markup=self.back()); return
        if a=="a_coupons":
            coupons=await list_coupons(owner)
            lines=["🎟 Coupon System\n", "Create: CODE | percent/fixed | VALUE | USAGE_LIMIT"]
            for cpn in coupons[:20]: lines.append(f"• {cpn['code']} — {cpn['value']:g} {cpn['discount_type']} — {cpn['used_count']}/{cpn['usage_limit']}")
            context.user_data.clear(); context.user_data["wait_coupon_create"]=True
            await q.edit_message_text("\n".join(lines),reply_markup=self.back()); return
        if a=="a_seller_referral":
            data=await seller_referral_stats(owner)
            link=f"https://t.me/{MAIN_BOT_USERNAME}?start=refseller_{owner}"
            await q.edit_message_text(
                "🤝 Seller Referral Program\n\n"
                f"👥 Sellers joined: {data['total']}\n"
                f"🎁 Rewards received: {data['rewarded']}\n\n"
                "Share this link with new sellers:\n"
                f"{link}\n\n"
                "The owner controls reward days and reward plan from Owner Dashboard → Subscription Management.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Share Referral Link",url=f"https://t.me/share/url?url={link}")],
                    [InlineKeyboardButton("⬅ Back",callback_data="a_home")],
                ]),
                disable_web_page_preview=True,
            ); return
        if a=="a_help":
            await q.edit_message_text(
                "📚 Clone Bot Admin Help Center\n\n"
                "🚀 Quick Start\n"
                "1️⃣ Add subscription plans\n"
                "2️⃣ Connect channel/group\n"
                "3️⃣ Configure UPI/QR or gateway\n"
                "4️⃣ Edit and preview welcome message\n"
                "5️⃣ Test payment, approval and invite link\n\n"
                "🛠 Commands\n"
                "/start — Seller opens Admin Panel; users open Welcome Menu\n"
                "/admin — Open Admin Panel\n"
                "/help — Full user and seller guide\n"
                "/connectgroup — Connect subscription group\n"
                "/connectsupport — Connect Live Support forum group\n"
                "/version — Show deployed runtime version\n\n"
                "📦 Plans — Add, edit, enable, disable or delete plans\n"
                "📂 Channels / Groups — Connect chats and resend links\n"
                "💳 Payments — UPI/QR, gateways, pending proofs and history\n"
                "👥 Users — Give, extend, remove, ban or unban\n"
                "💬 Welcome Editor — Text, media, buttons and preview\n"
                "🎫 Live Support — Topics, templates and auto remove\n"
                "📢 Broadcast — Send now, schedule and retry failed\n"
                "🎟 Coupons — Create and manage discounts\n"
                "🤝 Referral — User and seller referral controls\n"
                "📊 Statistics — Users, payments, plans and revenue\n\n"
                "🧪 Troubleshooting\n"
                "• Group not connecting: make bot admin and use /connectgroup inside it\n"
                "• Invite not sent: enable Invite Users permission\n"
                "• Live Support not working: enable forum topics and reconnect\n"
                "• Payment issue: verify UPI/QR or gateway credentials\n"
                "• Bot not replying: check runtime status and logs",
                reply_markup=self.back("a_home"),
            ); return
        if a=="a_terms":
            parts=[]
            for key in ("terms","privacy","refund","support"):
                policy=await get_policy(key); parts.append(f"{key.title()}:\n{policy.get('text')}")
            await q.edit_message_text("📜 Terms & Policy\n\n"+"\n\n".join(parts),reply_markup=self.admin_menu()); return
        if a=="a_broadcast": context.user_data.clear(); context.user_data["wait_broadcast"]=True; await q.edit_message_text("📢 Send any one message to broadcast.\n\nSupported: text, photo with caption, video, document, audio, voice, GIF, sticker and forwarded messages.",reply_markup=self.back()); return
        if a=="a_staff":
            await q.edit_message_text(
                "👮 Staff Management\n\nPromote trusted people as Admin or Moderator for this clone bot.\n\nAdmin: broad management access\nModerator: users, pending payments and live support",
                reply_markup=self.staff_menu(),
            )
            return
        if a in {"a_staff_add_admin", "a_staff_add_moderator"}:
            context.user_data.clear()
            context.user_data["wait_staff_promote"] = "admin" if a.endswith("admin") else "moderator"
            await q.edit_message_text(
                "Send the Telegram User ID of the person you want to promote.\n\nThe person must start this clone bot once before using staff access.",
                reply_markup=self.back("a_staff"),
            )
            return
        if a=="a_staff_list":
            rows=await list_staff(owner)
            if not rows:
                await q.edit_message_text("📋 Staff List\n\nNo staff members added.", reply_markup=self.back("a_staff"))
                return
            kb=[]
            lines=["📋 Staff List\n"]
            for row in rows:
                uid=int(row["user_id"]); role_name=str(row.get("role","moderator")).title(); status=row.get("status","active")
                label=("@"+row.get("username")) if row.get("username") else (row.get("full_name") or str(uid))
                lines.append(f"• {label} — {role_name} — {status.title()}")
                kb.append([InlineKeyboardButton(f"{role_name}: {label}", callback_data=f"a_staff_view_{uid}")])
            kb.append([InlineKeyboardButton("⬅ Back", callback_data="a_staff")])
            await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
            return
        if a.startswith("a_staff_view_"):
            uid=int(a.replace("a_staff_view_", "")); row=await active_staff(owner,uid)
            if not row:
                all_rows=await list_staff(owner); row=next((x for x in all_rows if int(x.get("user_id",0))==uid),None)
            if not row:
                await q.edit_message_text("❌ Staff member not found.", reply_markup=self.back("a_staff_list")); return
            label=("@"+row.get("username")) if row.get("username") else (row.get("full_name") or "Not available")
            text=(f"👮 Staff Details\n\nName: {label}\nUser ID: {uid}\nRole: {str(row.get('role','')).title()}\nStatus: {str(row.get('status','active')).title()}\nTotal Actions: {int(row.get('total_actions',0))}\nLast Action: {row.get('last_action') or 'No activity yet'}")
            await q.edit_message_text(text, reply_markup=self.staff_item_menu(uid,row.get("status")=="suspended"))
            return
        if a.startswith("a_staff_status_"):
            _,_,_,uid,status=a.split("_",4); await set_staff_status(owner,int(uid),status)
            await q.edit_message_text(f"✅ Staff status updated: {status.title()}", reply_markup=self.back("a_staff_list")); return
        if a.startswith("a_staff_remove_"):
            uid=int(a.replace("a_staff_remove_", "")); await remove_staff(owner,uid)
            await q.edit_message_text("✅ Staff member removed.", reply_markup=self.back("a_staff_list")); return

        if a=="a_users":
            context.user_data.clear()
            context.user_data["wait_user_search"]=True
            await q.edit_message_text(
                "👥 User Management\n\nSend User ID or @username to search.",
                reply_markup=self.back("a_home"),
            )
            return

        if a.startswith("a_user_view_"):
            await self.show_user_details(q,owner,int(a.replace("a_user_view_","")))
            return

        if a.startswith("a_user_give_"):
            await self.show_admin_plan_selector(
                q,owner,int(a.replace("a_user_give_","")),"give"
            )
            return

        if a.startswith("a_user_extend_"):
            await self.show_admin_plan_selector(
                q,owner,int(a.replace("a_user_extend_","")),"extend"
            )
            return

        if a.startswith("a_user_apply_"):
            parts=a.split("_",5)
            if len(parts)!=6:
                await q.edit_message_text("❌ Invalid action.")
                return

            mode=parts[3]
            user_id=int(parts[4])
            plan_id=parts[5]
            plan=await get_plan(owner,plan_id)

            if not plan:
                await q.edit_message_text(
                    "❌ Plan not found.",
                    reply_markup=self.back(f"a_user_view_{user_id}"),
                )
                return

            plan_cfg,_=await effective_plan(owner)
            active_now=await active_subscriptions(owner)
            already_active=any(int(x.get("user_id"))==user_id for x in active_now)
            sub_limit=int(plan_cfg.get("active_subscriber_limit",25))
            if not already_active and sub_limit>=0 and len(active_now)>=sub_limit:
                await q.edit_message_text(await plan_limit_warning(owner), reply_markup=self.limit_keyboard(f"a_user_view_{user_id}")); return
            await activate_subscription(
                owner,user_id,plan["name"],plan["duration_minutes"],
                amount=plan.get("price"),
                duration_text=plan.get("duration_text"),
            )

            delivery=await self.deliver_subscription_access(owner,user_id)
            try:
                await context.bot.send_message(
                    user_id,
                    "🎉 Subscription activated/extended by admin.\n"
                    f"Plan: {plan['name']}\n"
                    f"Duration added: {plan['duration_text']}\n\n"
                    f"New invite links sent: {delivery.get('sent',0)}\n"
                    f"Already joined: {delivery.get('already_member',0)}",
                )
            except Exception:
                pass

            await self.show_user_details(q,owner,user_id)
            return

        if a.startswith("a_user_remove_"):
            user_id=int(a.replace("a_user_remove_",""))
            await remove_subscription(owner,user_id)
            try:
                await context.bot.send_message(
                    user_id,
                    "❌ Your subscription was removed by admin.",
                )
            except Exception:
                pass
            await self.show_user_details(q,owner,user_id)
            return

        if a.startswith("a_user_ban_"):
            user_id=int(a.replace("a_user_ban_",""))
            context.user_data.clear()
            context.user_data["wait_user_ban_reason"]=user_id
            await q.edit_message_text(
                "🚫 Send ban reason.",
                reply_markup=self.back(f"a_user_view_{user_id}"),
            )
            return

        if a.startswith("a_user_unban_"):
            user_id=int(a.replace("a_user_unban_",""))
            await set_user_ban(owner,user_id,False,"")
            try:
                await context.bot.send_message(user_id,"✅ You have been unbanned.")
            except Exception:
                pass
            await self.show_user_details(q,owner,user_id)
            return

        if a=="a_stats":
            s=await stats(owner); await q.edit_message_text(f"📊 Statistics\n\nUsers: {s['users']}\nPlans: {s['plans']}\nChannels: {s['channels']}\nPending: {s['pending']}\nRevenue: ₹{s['revenue']:g}",reply_markup=self.admin_menu()); return

    @staticmethod
    def _support_datetime(value):
        if not value:
            return "-"
        if value.tzinfo is None:
            value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")

    async def support_user_details_text(self,owner,user):
        record=await get_user(owner,user.id) or {}
        sub=await get_subscription(owner,user.id) or {}
        expiry=sub.get("expiry_date")
        if expiry and expiry.tzinfo is None:
            expiry=expiry.replace(tzinfo=timezone.utc)
        active=bool(sub.get("active") and expiry and expiry>datetime.now(timezone.utc))
        full_name=html.escape(user.full_name or str(user.id))
        username=("@"+html.escape(user.username)) if user.username else "Not set"
        mention=f'<a href="tg://user?id={user.id}">{full_name}</a>'
        return (
            "🆕 <b>New Support User</b>\n\n"
            f"👤 Name: {full_name}\n"
            f"📝 Username: {username}\n"
            f"🆔 User ID: <code>{user.id}</code>\n"
            f"🔗 Mention: {mention}\n"
            f"🌐 Language: {html.escape(user.language_code or 'Unknown')}\n"
            f"📅 Joined: {self._support_datetime(record.get('joined_at'))}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💎 <b>Subscription</b>\n"
            f"Plan: {html.escape(str(sub.get('plan') or 'No Plan'))}\n"
            f"Status: {'✅ Active' if active else '❌ Inactive'}\n"
            f"Expiry: {self._support_datetime(expiry)}\n\n"
            "User ke Telegram profile par jane ke liye mention ya button use karo."
        )

    @staticmethod
    def support_topic_keyboard(user_id,blocked=False):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Open Telegram Profile",url=f"tg://user?id={int(user_id)}")],
            [InlineKeyboardButton("📋 View User Details",callback_data=f"support_profile_{int(user_id)}")],
            [InlineKeyboardButton(
                "✅ Unblock Support" if blocked else "🚫 Block Support",
                callback_data=(f"support_unblock_{int(user_id)}" if blocked else f"support_block_{int(user_id)}"),
            )],
            [InlineKeyboardButton("🆔 Show User ID",callback_data=f"support_id_{int(user_id)}")],
        ])

    async def ensure_support_topic(self,context,owner,user,support):
        topic=await get_support_topic(owner,user.id)
        group_id=int(support["support_group_id"])
        if topic and int(topic.get("support_group_id",0))==group_id:
            return topic
        topic_name=f"👤 {user.first_name or 'User'} | {user.id}"[:128]
        forum_topic=await context.bot.create_forum_topic(group_id,name=topic_name)
        topic=await save_support_topic(
            owner,user.id,group_id,forum_topic.message_thread_id,topic_name,
        )
        blocked=await is_support_blocked(owner,user.id)
        await context.bot.send_message(
            chat_id=group_id,
            message_thread_id=forum_topic.message_thread_id,
            text=await self.support_user_details_text(owner,user),
            parse_mode="HTML",
            reply_markup=self.support_topic_keyboard(user.id,blocked),
            disable_web_page_preview=True,
        )
        return topic

    async def support_template_values(self,owner,user):
        sub=await get_subscription(owner,user.id) or {}
        expiry=sub.get("expiry_date")
        values={
            "{NAME}":user.full_name or str(user.id),
            "{ID}":str(user.id),
            "{USERNAME}":("@"+user.username) if user.username else "",
            "{PLAN}":str(sub.get("plan") or "No Plan"),
            "{EXPIRY}":self._support_datetime(expiry),
        }
        return values

    async def send_support_template(self,context,owner,target_user_id,template,user_obj=None):
        if not template:
            raise ValueError("Template not found")
        if user_obj is None:
            record=await get_user(owner,target_user_id) or {}
            class UserView:
                id=int(target_user_id)
                full_name=" ".join(x for x in [record.get("first_name"),record.get("last_name")] if x) or str(target_user_id)
                username=record.get("username")
            user_obj=UserView()
        text=template.get("text") or ""
        for key,value in (await self.support_template_values(owner,user_obj)).items():
            text=text.replace(key,value)
        keyboard=self.build_welcome_keyboard(template.get("buttons") or [])
        file_id=template.get("media_file_id")
        media_type=template.get("media_type")
        kwargs={"chat_id":int(target_user_id),"reply_markup":keyboard}
        if file_id and media_type=="photo": sent=await context.bot.send_photo(photo=file_id,caption=text or None,**kwargs)
        elif file_id and media_type=="video": sent=await context.bot.send_video(video=file_id,caption=text or None,**kwargs)
        elif file_id and media_type=="animation": sent=await context.bot.send_animation(animation=file_id,caption=text or None,**kwargs)
        elif file_id and media_type=="document": sent=await context.bot.send_document(document=file_id,caption=text or None,**kwargs)
        else: sent=await context.bot.send_message(text=text or "(Empty template)",disable_web_page_preview=True,**kwargs)
        auto_delete_seconds=_template_auto_delete_seconds(template)
        if auto_delete_seconds > 0:
            asyncio.create_task(self._delete_template_message_later(context.bot,sent.chat_id,sent.message_id,auto_delete_seconds))
        return sent

    @staticmethod
    async def _delete_template_message_later(bot,chat_id,message_id,delay_seconds):
        try:
            await asyncio.sleep(max(1,int(delay_seconds)))
            await bot.delete_message(chat_id=chat_id,message_id=message_id)
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            logger.warning("Template auto-remove failed chat=%s message=%s: %s",chat_id,message_id,exc)
        except Exception:
            logger.exception("Unexpected template auto-remove failure chat=%s message=%s",chat_id,message_id)

    async def support_template_command_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        message=update.effective_message; user=update.effective_user; chat=update.effective_chat
        if not message or not user or user.id!=self.owner(context) or not message.text:
            return
        owner=self.owner(context); support=await get_live_support_settings(owner)
        command=message.text.split()[0].split("@",1)[0].lstrip("/").lower()
        template=await get_support_template(owner,command)
        if not template:
            return
        target_user_id=None
        if support.get("mode")=="topic" and support.get("support_group_id") and int(chat.id)==int(support["support_group_id"]) and message.message_thread_id:
            topic=await get_topic_by_thread(owner,chat.id,message.message_thread_id)
            if topic: target_user_id=int(topic["user_id"])
        elif support.get("mode")=="private" and chat.type=="private" and message.reply_to_message:
            link=await get_private_message_link(owner,chat.id,message.reply_to_message.message_id)
            if link: target_user_id=int(link["user_id"])
        if not target_user_id:
            await message.reply_text("❌ Is command ko user ke support topic me, ya private mode me user message ka reply karke bhejo.")
            raise ApplicationHandlerStop
        await self.send_support_template(context,owner,target_user_id,template)
        await message.reply_text(f"✅ /{command} sent to user")
        raise ApplicationHandlerStop

    async def route_live_support_message(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        message=update.effective_message
        user=update.effective_user
        chat=update.effective_chat
        if not message or not user or user.is_bot or not chat:
            return
        owner=self.owner(context)
        support=await get_live_support_settings(owner)

        # Seller reply inside the connected topic group.
        if (
            support.get("enabled") and support.get("mode")=="topic"
            and support.get("support_group_id")
            and int(chat.id)==int(support["support_group_id"])
            and message.message_thread_id
        ):
            if user.id!=owner:
                return
            topic=await get_topic_by_thread(owner,chat.id,message.message_thread_id)
            if not topic:
                return
            try:
                await context.bot.copy_message(
                    chat_id=int(topic["user_id"]),
                    from_chat_id=chat.id,
                    message_id=message.message_id,
                )
            except TelegramError as exc:
                logger.warning("Support topic reply failed owner=%s user=%s: %s",owner,topic.get("user_id"),exc)
            raise ApplicationHandlerStop

        # Seller reply in normal private mode must be a reply to a copied user message.
        if chat.type=="private" and user.id==owner:
            if support.get("enabled") and support.get("mode")=="private" and message.reply_to_message:
                link=await get_private_message_link(owner,chat.id,message.reply_to_message.message_id)
                if link:
                    await context.bot.copy_message(
                        chat_id=int(link["user_id"]),
                        from_chat_id=chat.id,
                        message_id=message.message_id,
                    )
                    raise ApplicationHandlerStop
            return

        # Users send any non-command message in private chat.
        if chat.type!="private" or user.id==owner:
            return
        if not support.get("enabled"):
            return
        special_states={
            "waiting_child_screenshot","wait_qr","wait_welcome_media","wait_broadcast",
            "wait_scheduled_broadcast","wait_channel","wait_plan_add","wait_plan_edit",
        }
        if any(context.user_data.get(key) for key in special_states):
            return
        if await is_support_blocked(owner,user.id):
            await message.reply_text("🚫 You cannot contact live support right now.")
            raise ApplicationHandlerStop

        await upsert_user(owner,user)
        mode=support.get("mode","topic")
        try:
            if mode=="topic":
                if not support.get("support_group_id"):
                    await message.reply_text("⚠️ Live support group is not connected yet. Please try again later.")
                    raise ApplicationHandlerStop
                try:
                    topic=await self.ensure_support_topic(context,owner,user,support)
                    await context.bot.copy_message(
                        chat_id=int(topic["support_group_id"]),
                        message_thread_id=int(topic["message_thread_id"]),
                        from_chat_id=chat.id,
                        message_id=message.message_id,
                    )
                except BadRequest as exc:
                    # Topic may have been manually deleted. Recreate it once.
                    logger.warning("Support topic stale owner=%s user=%s: %s",owner,user.id,exc)
                    await delete_support_topic(owner,user.id)
                    topic=await self.ensure_support_topic(context,owner,user,support)
                    await context.bot.copy_message(
                        chat_id=int(topic["support_group_id"]),
                        message_thread_id=int(topic["message_thread_id"]),
                        from_chat_id=chat.id,
                        message_id=message.message_id,
                    )
            else:
                header=await context.bot.send_message(
                    owner,
                    f"💬 Live Support\nUser: {user.full_name}\nID: {user.id}\nReply to the copied message below.",
                )
                copied=await context.bot.copy_message(
                    chat_id=owner,
                    from_chat_id=chat.id,
                    message_id=message.message_id,
                )
                await save_private_message_link(owner,owner,copied.message_id,user.id)
            await message.reply_text("✅ Message sent to live support.")
        except ApplicationHandlerStop:
            raise
        except TelegramError as exc:
            logger.exception("Live support routing failed owner=%s user=%s",owner,user.id)
            await message.reply_text(f"❌ Support message could not be sent: {str(exc)[:180]}")
        raise ApplicationHandlerStop

    async def connect_support_command(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)
        user=update.effective_user
        chat=update.effective_chat
        message=update.effective_message
        if not user or user.id!=owner:
            await message.reply_text("❌ Sirf Clone Bot seller/admin support group connect kar sakta hai.")
            return
        if not chat or chat.type!="supergroup" or not getattr(chat,"is_forum",False):
            await message.reply_text("❌ /connectsupport ko Topics ON wale private supergroup ke andar bhejo.")
            return
        try:
            me=await context.bot.get_me()
            member=await context.bot.get_chat_member(chat.id,me.id)
            if getattr(member,"status","") not in {"administrator","creator"}:
                await message.reply_text("❌ Clone Bot ko group Admin banao.")
                return
            if getattr(member,"status","")!="creator" and not getattr(member,"can_manage_topics",False):
                await message.reply_text("❌ Bot ke liye Manage Topics permission ON karo.")
                return
            updated=await update_live_support_settings(
                owner,
                support_group_id=chat.id,
                support_group_title=chat.title or "Support Group",
                mode="topic",
                enabled=True,
            )
            await message.reply_text(
                "✅ Support group connected successfully.\n\n"
                f"Group: {updated.get('support_group_title')}\n"
                "Live Support: ON\n"
                "Mode: Topic Mode\n\n"
                "Ab kisi user ka pehla message aate hi uske naam aur ID se naya topic banega."
            )
        except TelegramError as exc:
            logger.exception("Support group connection failed owner=%s chat=%s",owner,getattr(chat,"id",None))
            await message.reply_text(f"❌ Support group connect failed: {str(exc)[:200]}")

    async def support_callback(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        q=update.callback_query
        await q.answer()
        owner=self.owner(context)
        if q.from_user.id!=owner:
            await q.answer("Not authorized",show_alert=True)
            return
        data=q.data
        try:
            user_id=int(data.rsplit("_",1)[-1])
        except ValueError:
            return
        if data.startswith("support_id_"):
            await q.answer(f"User ID: {user_id}",show_alert=True); return
        if data.startswith("support_block_"):
            await set_support_block(owner,user_id,True)
            await q.edit_message_reply_markup(self.support_topic_keyboard(user_id,True)); return
        if data.startswith("support_unblock_"):
            await set_support_block(owner,user_id,False)
            await q.edit_message_reply_markup(self.support_topic_keyboard(user_id,False)); return
        if data.startswith("support_profile_"):
            text,record,sub=await self.user_details_text(owner,user_id)
            if not text:
                await q.answer("User not found",show_alert=True); return
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                message_thread_id=q.message.message_thread_id,
                text=text,
            )
            return

    async def text_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context); text=update.effective_message.text.strip()
        staff = await self.staff_record(update, context)
        if staff:
            gateway=context.user_data.get("wait_pg_credentials")
            if gateway:
                values=[x.strip() for x in text.split("|")]
                try:
                    if gateway=="razorpay" and len(values)==3:
                        payload={"key_id":values[0],"key_secret":values[1],"webhook_secret":values[2]}
                    elif gateway=="cashfree" and len(values)==2:
                        payload={"client_id":values[0],"client_secret":values[1]}
                    elif gateway=="phonepe" and len(values)==5:
                        payload={"client_id":values[0],"client_version":values[1],"client_secret":values[2],"webhook_username":values[3],"webhook_password":values[4]}
                    elif gateway=="paytm" and len(values)==3:
                        payload={"mid":values[0],"merchant_key":values[1],"website_name":values[2]}
                    else:
                        raise ValueError("Invalid credential format")
                    await save_gateway_config("seller",owner,gateway,payload)
                    context.user_data.clear()
                    await update.effective_message.reply_text("✅ Gateway credentials saved securely.",reply_markup=self.payment_menu())
                except Exception as exc:
                    await update.effective_message.reply_text(f"❌ {exc}")
                return
            if context.user_data.get("wait_support_tpl_command"):
                command=text.strip().lower().lstrip("/")
                try:
                    await save_support_template(owner,command)
                except Exception as exc:
                    await update.effective_message.reply_text(f"❌ {exc}"); return
                context.user_data.clear(); await update.effective_message.reply_text(f"✅ /{command} created",reply_markup=self.support_template_edit_menu(command)); return
            if context.user_data.get("wait_support_tpl_text"):
                command=context.user_data["wait_support_tpl_text"]
                await save_support_template(owner,command,text=text); context.user_data.clear()
                await update.effective_message.reply_text("✅ Template text saved",reply_markup=self.support_template_edit_menu(command)); return
            if context.user_data.get("wait_support_tpl_buttons"):
                command=context.user_data["wait_support_tpl_buttons"]
                try: rows=self.parse_welcome_buttons(text)
                except Exception as exc: await update.effective_message.reply_text(f"❌ {exc}"); return
                await save_support_template(owner,command,buttons=rows); context.user_data.clear()
                await update.effective_message.reply_text("✅ Template buttons saved",reply_markup=self.support_template_edit_menu(command)); return
            if context.user_data.get("wait_support_tpl_auto_delete"):
                command=context.user_data["wait_support_tpl_auto_delete"]
                try:
                    seconds=_parse_auto_delete_duration(text)
                    await save_support_template(owner,command,auto_delete_seconds=seconds)
                except Exception as exc:
                    await update.effective_message.reply_text(f"❌ {exc}")
                    return
                context.user_data.clear()
                await update.effective_message.reply_text(
                    f"✅ Template Auto Remove updated\n\n/{command}: {_format_auto_delete(seconds)}",
                    reply_markup=self.support_template_edit_menu(command),
                ); return
            if context.user_data.get("wait_coupon_create"):
                try:
                    code,ctype,value,limit=[x.strip() for x in text.split("|",3)]
                    if ctype not in {"percent","fixed"}: raise ValueError("type")
                    await create_coupon(owner,code,ctype,float(value),int(limit))
                    context.user_data.clear(); await update.effective_message.reply_text("✅ Coupon saved",reply_markup=self.admin_menu())
                except Exception:
                    await update.effective_message.reply_text("❌ Use: SAVE20 | percent | 20 | 100")
                return
            if context.user_data.get("wait_plan_add") or context.user_data.get("wait_plan_edit"):
                try:
                    name,dtext,dmins,price=self.parse_plan(text)
                    pid=context.user_data.get("wait_plan_edit")
                    if pid: await update_plan(owner,pid,name=name,duration_text=dtext,duration_minutes=dmins,price=price)
                    else: await create_plan(owner,name,dtext,dmins,price)
                    context.user_data.clear(); await update.effective_message.reply_text("✅ Plan saved",reply_markup=self.plans_admin_menu())
                except Exception as exc: await update.effective_message.reply_text(f"❌ {exc}")
                return
            if context.user_data.get("wait_channel"):
                try:
                    cid,name=[x.strip() for x in text.split("|",1)]; await add_channel(owner,int(cid),name,"group")
                    context.user_data.clear(); await update.effective_message.reply_text("✅ Channel/group added",reply_markup=self.channels_menu())
                except Exception: await update.effective_message.reply_text("❌ Use: -1001234567890 | Group Name")
                return
            mapping=[("wait_upi_id","upi_id",text,self.payment_menu()),("wait_upi_name","upi_name",text,self.payment_menu()),("wait_bot_name","bot_name",text,self.settings_menu()),("wait_support","support_username",text if text.startswith("@") else "@"+text,self.settings_menu()),("wait_currency","currency",text.upper(),self.settings_menu())]
            for state,key,val,kb in mapping:
                if context.user_data.get(state): await set_seller_setting(owner,key,val); context.user_data.clear(); await update.effective_message.reply_text("✅ Updated",reply_markup=kb); return
            if context.user_data.get("wait_welcome_text"):
                await set_seller_setting(owner,"welcome_message",text); context.user_data.clear()
                await update.effective_message.reply_text("✅ Welcome text saved. Use 👀 Preview to check it.",reply_markup=self.welcome_menu()); return
            if context.user_data.get("wait_welcome_buttons"):
                try: rows=self.parse_welcome_buttons(text)
                except Exception as exc: await update.effective_message.reply_text(f"❌ {exc}"); return
                await set_seller_setting(owner,"welcome_buttons",rows); context.user_data.clear()
                await update.effective_message.reply_text("✅ Welcome buttons saved. Use 👀 Preview to check them.",reply_markup=self.welcome_buttons_menu()); return
            if context.user_data.get("wait_staff_promote"):
                try:
                    staff_user_id=int(text.strip())
                    if staff_user_id==owner:
                        raise ValueError("Seller is already the owner")
                    user=await get_user(owner,staff_user_id)
                    role=context.user_data["wait_staff_promote"]
                    record=await promote_staff(
                        owner, staff_user_id, role, update.effective_user.id,
                        username=(user or {}).get("username", ""),
                        full_name=(user or {}).get("full_name", ""),
                    )
                    context.user_data.clear()
                    try:
                        await context.bot.send_message(staff_user_id, f"✅ You were promoted as {role.title()} for this clone bot. Send /start to open your staff panel.")
                    except Exception:
                        pass
                    await update.effective_message.reply_text(
                        f"✅ Staff promoted\n\nUser ID: {staff_user_id}\nRole: {role.title()}",
                        reply_markup=self.staff_menu(),
                    )
                except Exception as exc:
                    await update.effective_message.reply_text(f"❌ Could not promote staff: {exc}\n\nSend a numeric Telegram User ID.")
                return

            if context.user_data.get("wait_user_search"):
                query=text.strip()
                user=None

                if query.startswith("@"):
                    user=await get_user_by_username(owner,query)
                else:
                    try:
                        user=await get_user(owner,int(query))
                    except ValueError:
                        user=await get_user_by_username(owner,query)

                if not user:
                    await update.effective_message.reply_text(
                        "❌ User not found. Send a valid User ID or @username.",
                        reply_markup=self.back("a_home"),
                    )
                    return

                context.user_data.clear()

                class FakeQuery:
                    def __init__(self,message):
                        self.message=message
                    async def edit_message_text(self,text,reply_markup=None,**kwargs):
                        return await self.message.reply_text(text,reply_markup=reply_markup)

                await self.show_user_details(
                    FakeQuery(update.effective_message),
                    owner,
                    int(user["user_id"]),
                )
                return

            if context.user_data.get("wait_user_ban_reason"):
                user_id=int(context.user_data["wait_user_ban_reason"])
                await set_user_ban(owner,user_id,True,text)
                context.user_data.clear()

                try:
                    await context.bot.send_message(
                        user_id,
                        f"🚫 You have been banned.\nReason: {text}",
                    )
                except Exception:
                    pass

                class FakeQuery:
                    def __init__(self,message):
                        self.message=message
                    async def edit_message_text(self,text,reply_markup=None,**kwargs):
                        return await self.message.reply_text(text,reply_markup=reply_markup)

                await self.show_user_details(
                    FakeQuery(update.effective_message),
                    owner,
                    user_id,
                )
                return

            if context.user_data.get("wait_timezone"):
                try:
                    timezone_name = normalize_timezone(text)
                except Exception:
                    await update.effective_message.reply_text(
                        "❌ Invalid timezone.\n\nUse the exact format, for example:\nAsia/Kolkata\n\nTimezone names are case-sensitive.",
                        reply_markup=timezone_keyboard("a_tz_", "a_settings"),
                    )
                    return
                await set_seller_setting(owner, "timezone", timezone_name)
                context.user_data.clear()
                await update.effective_message.reply_text(
                    f"✅ Timezone updated!\n\nTimezone: {timezone_name}",
                    reply_markup=self.settings_menu(),
                )
                return
            if context.user_data.get("wait_referral_days"):
                try:
                    days=int(text)
                    if days < 0 or days > 3650:
                        raise ValueError
                except ValueError:
                    await update.effective_message.reply_text(
                        "❌ Send a number from 0 to 3650."
                    )
                    return

                await set_seller_setting(
                    owner,
                    "referral_reward_days",
                    days,
                )
                context.user_data.clear()
                await update.effective_message.reply_text(
                    f"✅ Referral reward set to {days} day(s).",
                    reply_markup=self.settings_menu(),
                )
                return
            if context.user_data.get("wait_reminder"):
                try: days=int(text)
                except ValueError: await update.effective_message.reply_text("❌ Send number"); return
                await set_seller_setting(owner,"reminder_days",days); context.user_data.clear(); await update.effective_message.reply_text("✅ Updated",reply_markup=self.settings_menu()); return

    async def broadcast_message_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)

        if update.effective_user.id!=owner:
            return

        if context.user_data.get("wait_scheduled_broadcast"):
            raw=(update.effective_message.text or update.effective_message.caption or "").strip()
            lines=raw.splitlines()
            try:
                run_local=datetime.strptime(lines[0].strip(),"%Y-%m-%d %H:%M")
                settings=await get_seller_settings(owner)
                zone=ZoneInfo(settings.get("timezone","Asia/Kolkata"))
                run_at=run_local.replace(tzinfo=zone).astimezone(timezone.utc)
                if run_at<=datetime.now(timezone.utc): raise ValueError("past")
            except Exception:
                await update.effective_message.reply_text("❌ First line must be a future time: YYYY-MM-DD HH:MM")
                return
            job=await save_scheduled_broadcast(owner,run_at,update.effective_chat.id,update.effective_message.message_id)
            context.application.job_queue.run_once(self.scheduled_broadcast_job,when=run_at,data=job,name=f"scheduled_{job['job_id']}")
            context.user_data.clear(); await update.effective_message.reply_text(f"✅ Broadcast scheduled for {run_local:%d-%m-%Y %I:%M %p}",reply_markup=self.admin_menu())
            raise ApplicationHandlerStop

        if not context.user_data.get("wait_broadcast"):
            return

        from database.seller_data import c, USERS

        users=await c(USERS).find(
            {"owner_id":owner},
            {"user_id":1},
        ).to_list(length=None)

        success=0
        failed=0

        for user in users:
            user_id=user.get("user_id")
            if not user_id or user_id==owner:
                continue

            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.effective_message.message_id,
                )
                success+=1
            except Exception:
                failed+=1

        context.user_data.clear()

        await update.effective_message.reply_text(
            "✅ Broadcast completed\n\n"
            f"Success: {success}\n"
            f"Failed: {failed}",
            reply_markup=self.admin_menu(),
        )

        raise ApplicationHandlerStop

    async def restore_scheduled_broadcasts(
        self,
        application: Application,
        owner_id: int,
    ):
        """
        Restore database-backed broadcasts after a clone-bot restart.

        JobQueue entries are memory-only, so pending database jobs must be
        registered again whenever the clone bot starts.
        """
        jobs = await pending_scheduled_broadcasts(owner_id)
        now = datetime.now(timezone.utc)

        for job in jobs:
            run_at = job.get("run_at") or now
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)

            existing = application.job_queue.get_jobs_by_name(
                f"scheduled_{job['job_id']}"
            )
            if existing:
                continue

            application.job_queue.run_once(
                self.scheduled_broadcast_job,
                when=max(run_at, now),
                data=job,
                name=f"scheduled_{job['job_id']}",
            )

        if jobs:
            logger.info(
                "Restored scheduled broadcasts owner_id=%s count=%s",
                owner_id,
                len(jobs),
            )

    async def scheduled_broadcast_job(
        self,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        job = context.job.data
        job_id = job["job_id"]
        owner = int(job["owner_id"])

        claimed = await claim_scheduled_broadcast(job_id)
        if not claimed:
            logger.info(
                "Scheduled broadcast skipped because it was already claimed "
                "job_id=%s owner_id=%s",
                job_id,
                owner,
            )
            return

        try:
            from database.seller_data import c, USERS

            users = await c(USERS).find(
                {"owner_id": owner},
                {"user_id": 1},
            ).to_list(length=None)

            success = failed = 0

            for user in users:
                if await broadcast_cancel_requested(job_id):
                    logger.info(
                        "Scheduled broadcast cancellation observed "
                        "job_id=%s owner_id=%s",
                        job_id,
                        owner,
                    )
                    break

                uid = user.get("user_id")
                if not uid or uid == owner:
                    continue

                try:
                    await context.bot.copy_message(
                        uid,
                        job["from_chat_id"],
                        job["message_id"],
                    )
                    success += 1
                except Exception as exc:
                    failed += 1
                    await save_failed_delivery(
                        owner,
                        uid,
                        "scheduled_broadcast",
                        {"job_id": job_id},
                        str(exc),
                    )

                await asyncio.sleep(0.05)

            await set_scheduled_status(
                job_id,
                "completed",
                {"success": success, "failed": failed},
            )

            try:
                await context.bot.send_message(
                    owner,
                    "✅ Scheduled broadcast completed\n"
                    f"Success: {success}\n"
                    f"Failed: {failed}",
                )
            except Exception:
                logger.exception(
                    "Scheduled broadcast completion notice failed "
                    "job_id=%s owner_id=%s",
                    job_id,
                    owner,
                )
        except Exception as exc:
            logger.exception(
                "Scheduled broadcast execution failed "
                "job_id=%s owner_id=%s",
                job_id,
                owner,
            )
            await release_scheduled_broadcast(job_id, str(exc))

    async def welcome_media_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)
        if update.effective_user.id!=owner:
            return
        if context.user_data.get("wait_support_tpl_media"):
            command=context.user_data["wait_support_tpl_media"]
            msg=update.effective_message; media_type=""; file_id=""
            if msg.photo: media_type="photo"; file_id=msg.photo[-1].file_id
            elif msg.video: media_type="video"; file_id=msg.video.file_id
            elif msg.animation: media_type="animation"; file_id=msg.animation.file_id
            elif msg.document: media_type="document"; file_id=msg.document.file_id
            if not file_id: await msg.reply_text("❌ Photo, video, GIF ya document bhejo."); return
            await save_support_template(owner,command,media_type=media_type,media_file_id=file_id)
            context.user_data.clear(); await msg.reply_text("✅ Template media saved",reply_markup=self.support_template_edit_menu(command))
            raise ApplicationHandlerStop
        if not context.user_data.get("wait_welcome_media"): return
        msg=update.effective_message; media_type=""; file_id=""
        if msg.photo: media_type="photo"; file_id=msg.photo[-1].file_id
        elif msg.video: media_type="video"; file_id=msg.video.file_id
        elif msg.animation: media_type="animation"; file_id=msg.animation.file_id
        elif msg.document: media_type="document"; file_id=msg.document.file_id
        if not file_id: await msg.reply_text("❌ Send photo, video, GIF or document."); return
        await set_seller_setting(owner,"welcome_media_type",media_type)
        await set_seller_setting(owner,"welcome_media_file_id",file_id)
        context.user_data.clear(); await msg.reply_text("✅ Welcome media saved. Use 👀 Preview to check it.",reply_markup=self.welcome_menu())
        raise ApplicationHandlerStop

    async def photo_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)

        if update.effective_user.id==owner and context.user_data.get("wait_qr"):
            await set_seller_setting(owner,"upi_qr_file_id",update.effective_message.photo[-1].file_id); context.user_data.clear(); await update.effective_message.reply_text("✅ QR updated",reply_markup=self.payment_menu()); return
        if context.user_data.get("waiting_child_screenshot"):
            plan=context.user_data.get("selected_child_plan")
            if not plan: await update.effective_message.reply_text("Select a plan first"); return
            photo=update.effective_message.photo[-1]
            unique=getattr(photo,"file_unique_id","")
            if not await reserve_payment_fingerprint("child",owner,unique,update.effective_user.id):
                context.user_data.clear(); await update.effective_message.reply_text("⚠️ This payment screenshot was already submitted. Send a new genuine payment proof."); return
            p=await create_payment(owner,update.effective_user.id,plan,photo.file_id); context.user_data.clear()
            await audit("child_payment_submitted",update.effective_user.id,owner,{"payment_id":p.get("payment_id")})
            kb=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"a_pay_ok_{p['payment_id']}",
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=f"a_pay_no_{p['payment_id']}",
                    ),
                ]
            ])

            caption=await self.payment_details_caption(
                owner,
                p,
                status="pending",
            )

            await context.bot.send_photo(
                owner,
                p["screenshot_file_id"],
                caption=caption,
                reply_markup=kb,
            )

            await update.effective_message.reply_text(
                "✅ Payment submitted. Waiting for approval."
            )
            raise ApplicationHandlerStop

    async def forward_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)
        if update.effective_user.id!=owner or not context.user_data.get("wait_channel"): return
        m=update.effective_message; chat=getattr(m,"forward_from_chat",None)
        if chat is None:
            origin=getattr(m,"forward_origin",None); chat=getattr(origin,"chat",None)
        if chat is None:
            await m.reply_text(
                "❌ Forward se group detect nahi hua.\n\n"
                "Easy method: child bot ko group me Admin banao, phir group ke andar /connectgroup bhejo."
            )
            return
        await add_channel(owner,chat.id,chat.title or "Unknown",getattr(chat,"type","unknown")); context.user_data.clear(); await m.reply_text("✅ Channel/group added",reply_markup=self.channels_menu())


    async def connect_group_command(self, update:Update, context:ContextTypes.DEFAULT_TYPE):
        """Connect the current private/super group without asking for a numeric chat id."""
        owner=self.owner(context)
        user=update.effective_user
        chat=update.effective_chat
        message=update.effective_message

        if not user or user.id != owner:
            await message.reply_text("❌ Sirf bot seller/admin group connect kar sakta hai.")
            return
        if not chat or chat.type not in {"group", "supergroup"}:
            await message.reply_text(
                "❌ Ye command target group ke andar bhejo.\n\n"
                "Child bot ko group me add karke Admin banao, phir /connectgroup send karo."
            )
            return

        try:
            me=await context.bot.get_me()
            member=await context.bot.get_chat_member(chat.id, me.id)
            status=getattr(member, "status", "")
            can_invite=getattr(member, "can_invite_users", False)
            if status not in {"administrator", "creator"}:
                await message.reply_text(
                    "❌ Pehle child bot ko is group ka Admin banao.\n"
                    "Invite Users permission bhi ON rakho."
                )
                return
            if status != "creator" and not can_invite:
                await message.reply_text(
                    "❌ Bot ke paas Invite Users permission nahi hai.\n"
                    "Group Admin settings me Invite Users permission ON karo, phir /connectgroup dobara bhejo."
                )
                return

            await add_channel(owner, chat.id, chat.title or "Premium Group", chat.type)

            # Confirm that Telegram can actually generate an invite for this chat.
            invite=await context.bot.create_chat_invite_link(
                chat_id=chat.id,
                member_limit=1,
                name="Connection test",
            )
            try:
                await context.bot.revoke_chat_invite_link(chat.id, invite.invite_link)
            except Exception:
                pass

            await message.reply_text(
                "✅ Group connected successfully.\n\n"
                f"Group: {chat.title or 'Premium Group'}\n"
                "Invite-link permission: Working ✅\n\n"
                "Ab payment approve hone par active user ko fresh invite link milega."
            )
            context.user_data.clear()
        except BadRequest as exc:
            logger.warning("Group connect failed owner=%s chat=%s: %s", owner, getattr(chat,'id',None), exc)
            await message.reply_text(
                "❌ Group save nahi hua ya invite link create nahi ho saka.\n\n"
                "Check karo:\n"
                "• Bot group me Admin ho\n"
                "• Invite Users permission ON ho\n"
                "• Group supergroup/private group ho\n\n"
                f"Telegram error: {exc}"
            )
        except Exception as exc:
            logger.exception("Unexpected group connect error owner=%s", owner)
            await message.reply_text(f"❌ Group connect failed: {exc}")

    async def deliver_subscription_access(self, owner_id:int, user_id:int):
        """Send fresh invite links only for chats the user has not joined yet."""
        running=self.get_running(int(owner_id))
        if not running:
            record=await get_bot_by_data_owner_id(int(owner_id))
            started=await self.start_bot(int(record["bot_id"])) if record else False
            running=self.get_running(int(owner_id)) if started else None
        if not running:
            return {"sent":0,"already_member":0,"failed":0,"error":"Clone bot is not running"}

        bot=running.application.bot
        channels=await get_channels(int(owner_id))
        links=[]
        already_member=0
        failed=0

        for ch in channels:
            chat_id=int(ch["chat_id"])
            try:
                member=await bot.get_chat_member(chat_id,int(user_id))
                status=getattr(member,"status","")
                is_member=getattr(member,"is_member",None)
                if status in {"creator","administrator","member"} or (status=="restricted" and is_member is not False):
                    already_member+=1
                    continue
                if status=="kicked":
                    try:
                        await bot.unban_chat_member(chat_id,int(user_id),only_if_banned=True)
                    except TelegramError:
                        pass
            except BadRequest:
                pass
            except TelegramError as exc:
                logger.warning("Membership check failed owner=%s chat=%s user=%s: %s",owner_id,chat_id,user_id,exc)

            try:
                invite=await bot.create_chat_invite_link(
                    chat_id=chat_id,
                    member_limit=1,
                    name=f"Subscription access {user_id}",
                )
                await save_invite(owner_id, user_id, chat_id, invite.invite_link)
                links.append(f"📢 {ch.get('title','Premium Channel/Group')}\n{invite.invite_link}")
            except TelegramError as exc:
                failed+=1
                logger.warning("Invite creation failed owner=%s chat=%s user=%s: %s",owner_id,chat_id,user_id,exc)

        if links:
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text=(
                        "✅ Your subscription has been updated.\n\n"
                        "Use the fresh invite link(s) below to join the channel/group(s) you have not joined yet:\n\n"
                        + "\n\n".join(links)
                    ),
                    disable_web_page_preview=True,
                )
            except TelegramError as exc:
                return {"sent":0,"already_member":already_member,"failed":failed+len(links),"error":str(exc)}

        return {"sent":len(links),"already_member":already_member,"failed":failed,"error":""}

    async def expiry_job(self,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)

        for sub in await expired_subscriptions(owner):
            uid=sub["user_id"]

            for invite_doc in await active_invites_for_user(owner, uid):
                try:
                    await context.bot.revoke_chat_invite_link(
                        int(invite_doc["chat_id"]), invite_doc["invite_link"]
                    )
                except Exception:
                    pass
                await deactivate_invite(owner, invite_doc["invite_link"])

            for ch in await get_channels(owner):
                try:
                    await context.bot.ban_chat_member(
                        ch["chat_id"],
                        uid,
                    )
                    await context.bot.unban_chat_member(
                        ch["chat_id"],
                        uid,
                        only_if_banned=True,
                    )
                except Exception:
                    pass

            await mark_expired(owner,uid)

            keyboard=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Renew Plan",
                        callback_data="c_renew",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "👤 My Profile",
                        callback_data="c_profile",
                    )
                ],
            ])

            try:
                await context.bot.send_message(
                    uid,
                    "⏰ Your subscription has expired.\n\n"
                    "Access to premium channel/group has been removed.\n\n"
                    "Use 🔄 Renew Plan to continue.",
                    reply_markup=keyboard,
                )
            except Exception:
                pass

    async def clone_error_handler(self, update, context):
        bot_id = context.application.bot_data.get("seller_bot_id")
        owner_id = context.application.bot_data.get("seller_owner_id")
        logger.error(
            "Unhandled clone bot update error bot_id=%s owner_id=%s",
            bot_id,
            owner_id,
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )

    def build_app(self,token,data_owner_id,seller_account_id,bot_id=None):
        protected_bot=ProtectedExtBot(token=token,owner_id=int(data_owner_id))
        app=Application.builder().bot(protected_bot).build()
        app.bot_data["seller_owner_id"]=int(data_owner_id)
        app.bot_data["seller_account_id"]=int(seller_account_id)
        app.bot_data["seller_bot_id"]=int(bot_id or 0)
        app.add_error_handler(self.clone_error_handler)
        app.add_handler(CommandHandler("start",self.child_start))
        app.add_handler(CommandHandler("help",self.help_command))
        app.add_handler(CommandHandler("admin",self.admin))
        app.add_handler(CommandHandler("connectgroup",self.connect_group_command))
        app.add_handler(CommandHandler("connectsupport",self.connect_support_command))
        app.add_handler(MessageHandler(filters.COMMAND,self.support_template_command_handler),group=9)
        app.add_handler(
            CommandHandler(
                "version",
                lambda update,context: update.effective_message.reply_text(
                    f"Runtime: {WELCOME_RUNTIME_VERSION}"
                ),
            )
        )
        app.add_handler(CallbackQueryHandler(self.child_callback,pattern=r"^c_")); app.add_handler(CallbackQueryHandler(self.admin_callback,pattern=r"^a_"))
        app.add_handler(CallbackQueryHandler(self.support_callback,pattern=r"^support_"))
        for handler in deleting_messages_handlers():
            app.add_handler(handler,group=-7)
        for handler in content_protection_handlers():
            app.add_handler(handler,group=-7)
        for handler in subscription_guard_handlers():
            app.add_handler(handler,group=-7)
        app.add_handler(ChatMemberHandler(subscription_guard_chat_member, ChatMemberHandler.CHAT_MEMBER), group=-30)
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, subscription_guard_new_members), group=-29)
        app.add_handler(MessageHandler(filters.ALL,moderate_seller_message),group=-20)
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,self.broadcast_message_handler),group=-3)
        app.add_handler(MessageHandler(filters.FORWARDED,self.forward_handler),group=-2)
        app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,self.welcome_media_handler),group=-1)
        app.add_handler(MessageHandler(filters.PHOTO,self.photo_handler),group=0)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,self.text_handler))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,self.route_live_support_message),group=10)
        if app.job_queue: app.job_queue.run_repeating(self.expiry_job,interval=60,first=30,name=f"seller_expiry_{data_owner_id}")
        return app

    async def start_bot(self, bot_id: int) -> bool:
        bot_id = int(bot_id)
        async with self._lock_for(bot_id):
            if bot_id in self._running:
                running = self._running[bot_id]
                if running.application.running and (
                    not running.application.updater or running.application.updater.running
                ):
                    return True
                self._running.pop(bot_id, None)

            record = await get_bot_by_bot_id(bot_id)
            if not record:
                record = await get_bot(bot_id)
            if not record or not record.get("active"):
                return False

            bot_id = int(record["bot_id"])
            seller_account_id = int(record["owner_id"])
            allowed, quota = await bot_runtime_allowed(seller_account_id, bot_id)
            if not allowed:
                limit = quota.get("limit", 0)
                position = quota.get("position")
                reason = (
                    f"Clone bot position {position} exceeds seller plan limit {limit}"
                    if position is not None
                    else f"Seller plan allows {limit} clone bots"
                )
                await set_runtime_status(bot_id, "plan_limit_paused", reason)
                logger.warning(
                    "Clone bot blocked by seller plan bot_id=%s owner_id=%s position=%s limit=%s",
                    bot_id, seller_account_id, position, limit,
                )
                return False

            token = await get_decrypted_bot_token(bot_id)
            if not token:
                await set_runtime_status(bot_id, "token_missing", "Missing encrypted token")
                return False

            app: Optional[Application] = None
            data_owner_id = int(record.get("data_owner_id") or record["owner_id"])
            try:
                await asyncio.wait_for(
                    ensure_seller_defaults(data_owner_id, record.get("bot_name", "Subscription Bot")),
                    timeout=20,
                )
                app = self.build_app(token, data_owner_id, seller_account_id, bot_id=bot_id)
                await asyncio.wait_for(app.initialize(), timeout=25)
                await asyncio.wait_for(app.start(), timeout=15)
                await asyncio.wait_for(
                    app.updater.start_polling(
                        drop_pending_updates=True,
                        allowed_updates=Update.ALL_TYPES,
                        bootstrap_retries=-1,
                    ),
                    timeout=35,
                )
                self._running[bot_id] = RunningSellerBot(
                    data_owner_id,
                    bot_id,
                    app,
                )
                await set_runtime_status(bot_id, "running", None)

                try:
                    await self.restore_scheduled_broadcasts(
                        app,
                        data_owner_id,
                    )
                except Exception:
                    logger.exception(
                        "Scheduled broadcast restoration failed "
                        "bot_id=%s owner_id=%s",
                        bot_id,
                        data_owner_id,
                    )

                logger.info(
                    "Clone bot started bot_id=%s owner_id=%s",
                    bot_id,
                    data_owner_id,
                )
                return True
            except Exception as exc:
                logger.exception("Seller bot start failed bot_id=%s", bot_id)
                try:
                    await set_runtime_status(bot_id, "error", str(exc)[:500])
                except Exception:
                    logger.exception("Could not save clone bot failure status bot_id=%s", bot_id)
                if app:
                    await self._safe_shutdown(app)
                return False

    async def _safe_shutdown(self, app):
        try:
            if app.updater and app.updater.running:
                await asyncio.wait_for(app.updater.stop(), timeout=15)
        except Exception:
            logger.debug("Clone updater stop failed", exc_info=True)
        try:
            if app.running:
                await asyncio.wait_for(app.stop(), timeout=15)
        except Exception:
            logger.debug("Clone application stop failed", exc_info=True)
        try:
            await asyncio.wait_for(app.shutdown(), timeout=15)
        except Exception:
            logger.debug("Clone application shutdown failed", exc_info=True)

    async def stop_bot(self, bot_id: int, runtime_status="paused"):
        bot_id = int(bot_id)
        async with self._lock_for(bot_id):
            running = self._running.pop(bot_id, None)
            if running is None:
                matched_id = next(
                    (
                        rid
                        for rid, item in self._running.items()
                        if int(item.owner_id) == bot_id
                        or int(item.application.bot_data.get("seller_account_id", -1)) == bot_id
                    ),
                    None,
                )
                if matched_id is not None:
                    bot_id = int(matched_id)
                    running = self._running.pop(bot_id, None)
            if running:
                await self._safe_shutdown(running.application)
            await set_runtime_status(bot_id, runtime_status, None)
            return True

    async def restart_bot(self, bot_id):
        await self.stop_bot(bot_id, "restarting")
        return await self.start_bot(bot_id)

    async def _restore_one(self, bot_id: int) -> bool:
        async with self._restore_semaphore:
            return await self.start_bot(bot_id)

    async def restore_active_bots(self):
        records = await get_all_active_bots()
        if not records:
            return {"started": 0, "failed": 0}
        results = await asyncio.gather(
            *(self._restore_one(int(record["bot_id"])) for record in records),
            return_exceptions=True,
        )
        started = sum(result is True for result in results)
        failed = len(results) - started
        for result in results:
            if isinstance(result, Exception):
                logger.error("Clone bot restore task failed", exc_info=(type(result), result, result.__traceback__))
        return {"started": started, "failed": failed}

    async def _recover_bot_with_retry(
        self,
        bot_id: int,
        *,
        max_attempts: int = 3,
        delays=(2, 5, 10),
    ) -> bool:
        """Recover one clone bot with bounded retries and recovery metrics."""
        bot_id = int(bot_id)
        last_error = ""
        record = await get_bot_by_bot_id(bot_id)
        if not record or not await recovery_allowed(record):
            return False
        claim = await claim_runtime_recovery(bot_id, cooldown_seconds=300)
        if not claim:
            return False

        for attempt in range(1, max_attempts + 1):
            self._recovery_attempts[bot_id] = attempt
            self._recovery_totals[bot_id] = self._recovery_totals.get(bot_id, 0) + 1
            logger.warning(
                "[RECOVERY] clone bot restart bot_id=%s attempt=%s/%s",
                bot_id,
                attempt,
                max_attempts,
            )

            try:
                recovered = await self.restart_bot(bot_id)
                if recovered:
                    now = datetime.now(timezone.utc)
                    self._last_recovery_at[bot_id] = now
                    self._last_recovery_error.pop(bot_id, None)
                    self._recovery_attempts[bot_id] = 0
                    logger.info(
                        "[RECOVERY] clone bot restored bot_id=%s attempt=%s time=%s",
                        bot_id,
                        attempt,
                        now.isoformat(),
                    )
                    await finish_runtime_recovery(bot_id, True)
                    return True
                last_error = "restart_bot returned False"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = str(exc)[:500]
                logger.exception(
                    "[RECOVERY] clone bot restart failed bot_id=%s attempt=%s",
                    bot_id,
                    attempt,
                )

            self._last_failure_at[bot_id] = datetime.now(timezone.utc)
            self._last_recovery_error[bot_id] = last_error
            if attempt < max_attempts:
                delay = delays[min(attempt - 1, len(delays) - 1)]
                await asyncio.sleep(max(0, delay))

        self._recovery_attempts[bot_id] = 0
        try:
            failures = int((claim or {}).get("consecutive_recovery_failures", 0)) + 1
            retry_after = min(3600, 300 * (2 ** min(failures - 1, 3)))
            await finish_runtime_recovery(
                bot_id, False, last_error[:500], retry_after_seconds=retry_after
            )
        except Exception:
            logger.exception(
                "Could not save recovery failure status bot_id=%s",
                bot_id,
            )
        return False

    async def recover_dead_bots(self):
        """Recover stopped and unexpectedly missing active clone-bot runtimes."""
        if self._watchdog_lock.locked():
            return {
                "checked": len(self._running),
                "candidates": 0,
                "restarted": 0,
                "failed": 0,
                "skipped": True,
            }

        async with self._watchdog_lock:
            records = await get_all_active_bots()
            active_ids = {
                int(record["bot_id"])
                for record in records
                if record.get("bot_id") is not None
            }

            # Remove stale in-memory entries for bots that are no longer active.
            stale_ids = [
                int(bot_id)
                for bot_id in list(self._running)
                if int(bot_id) not in active_ids
            ]
            for bot_id in stale_ids:
                running = self._running.pop(bot_id, None)
                if running:
                    await self._safe_shutdown(running.application)

            candidates = []
            for bot_id in active_ids:
                running = self._running.get(bot_id)
                if running is None:
                    candidates.append(bot_id)
                    continue

                app = running.application
                updater_running = bool(app.updater and app.updater.running)
                if not app.running or not updater_running:
                    candidates.append(bot_id)

            if not candidates:
                return {
                    "checked": len(active_ids),
                    "candidates": 0,
                    "restarted": 0,
                    "failed": 0,
                    "stale_removed": len(stale_ids),
                    "skipped": False,
                }

            logger.warning(
                "Clone bot watchdog recovery candidates: %s",
                candidates,
            )
            results = await asyncio.gather(
                *(self._recover_bot_with_retry(bot_id) for bot_id in candidates),
                return_exceptions=True,
            )

            restarted = sum(result is True for result in results)
            failed = len(results) - restarted
            for bot_id, result in zip(candidates, results):
                if isinstance(result, Exception):
                    self._last_failure_at[bot_id] = datetime.now(timezone.utc)
                    self._last_recovery_error[bot_id] = str(result)[:500]
                    logger.error(
                        "Clone bot watchdog task failed bot_id=%s",
                        bot_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )

            return {
                "checked": len(active_ids),
                "candidates": len(candidates),
                "restarted": restarted,
                "failed": failed,
                "stale_removed": len(stale_ids),
                "skipped": False,
            }

    async def runtime_health(self):
        """Return clone-bot runtime information for health endpoints."""
        records = await get_all_active_bots()
        active_ids = {
            int(record["bot_id"])
            for record in records
            if record.get("bot_id") is not None
        }
        running_ids = set()
        unhealthy_ids = []

        for bot_id, running in list(self._running.items()):
            app = running.application
            healthy = bool(
                app.running
                and app.updater
                and app.updater.running
            )
            if healthy:
                running_ids.add(int(bot_id))
            else:
                unhealthy_ids.append(int(bot_id))

        offline_ids = sorted(active_ids - running_ids)
        all_metric_ids = active_ids | set(self._recovery_totals)
        recovery_total = sum(
            self._recovery_totals.get(bot_id, 0)
            for bot_id in all_metric_ids
        )

        def iso(value):
            return value.isoformat() if value else None

        return {
            "active": len(active_ids),
            "running": len(running_ids),
            "offline": len(offline_ids),
            "unhealthy": len(unhealthy_ids),
            "offline_bot_ids": offline_ids,
            "unhealthy_bot_ids": sorted(unhealthy_ids),
            "recovery_attempts_total": recovery_total,
            "currently_recovering": sorted(
                bot_id
                for bot_id, attempt in self._recovery_attempts.items()
                if attempt > 0
            ),
            "last_recovery_at": {
                str(bot_id): iso(value)
                for bot_id, value in self._last_recovery_at.items()
            },
            "last_failure_at": {
                str(bot_id): iso(value)
                for bot_id, value in self._last_failure_at.items()
            },
            "last_errors": {
                str(bot_id): error
                for bot_id, error in self._last_recovery_error.items()
                if error
            },
        }

    async def shutdown_all(self):
        bot_ids = list(self._running)
        if bot_ids:
            await asyncio.gather(
                *(self.stop_bot(bot_id, "service_stopped") for bot_id in bot_ids),
                return_exceptions=True,
            )

bot_manager=SellerBotManager()
