import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict, InvalidToken, TelegramError
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database.seller_subscriptions import effective_plan, plan_limit_warning, current_plan_text, get_config, seller_access_state, usage_warning
from database.seller_bots import get_all_active_bots, get_bot, get_decrypted_bot_token, set_runtime_status
from database.seller_data import (
    activate_subscription, active_subscriptions, add_channel, create_payment, create_plan, delete_plan,
    ensure_seller_defaults, expired_subscriptions, get_channels, get_payment,
    get_plan, get_plans, get_seller_settings, get_subscription, get_user, mark_expired,
    payment_history, pending_payments, remove_channel, set_payment_status,
    claim_payment_for_processing, finalize_processed_payment,
    release_processing_payment,
    set_seller_setting, stats, update_plan, upsert_user,
    register_referral, count_all_referrals, count_successful_referrals,
    mark_referral_rewarded, get_user_by_username, set_user_ban,
    remove_subscription,
)

logger=logging.getLogger(__name__)
WELCOME_RUNTIME_VERSION="2026-07-13-main-role-dashboard-fix-13"
MAIN_BOT_USERNAME=os.getenv("MAIN_BOT_USERNAME","Local_supplier3_bot").lstrip("@")

@dataclass
class RunningSellerBot:
    owner_id:int; bot_id:int; application:Application

class SellerBotManager:
    def __init__(self): self._running:Dict[int,RunningSellerBot]={}; self._lock=asyncio.Lock()
    def is_running(self,owner_id:int)->bool:return owner_id in self._running

    @staticmethod
    def main_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Plans",callback_data="c_plans"),InlineKeyboardButton("💳 Buy",callback_data="c_buy")],
            [InlineKeyboardButton("👤 My Profile",callback_data="c_profile"),InlineKeyboardButton("🔄 Renew",callback_data="c_renew")],
            [InlineKeyboardButton("🎁 Referral",callback_data="c_referral"),InlineKeyboardButton("📞 Support",callback_data="c_support")],
        ])
    @staticmethod
    def admin_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Manage Plans",callback_data="a_plans")],
            [InlineKeyboardButton("📢 Channels / Groups",callback_data="a_channels")],
            [InlineKeyboardButton("💳 Payment Settings",callback_data="a_payment")],
            [InlineKeyboardButton("📨 Pending Payments",callback_data="a_pending")],
            [InlineKeyboardButton("📜 Payment History",callback_data="a_history")],
            [InlineKeyboardButton("⚙️ Bot Settings",callback_data="a_settings")],
            [InlineKeyboardButton("📢 Broadcast",callback_data="a_broadcast")],
            [InlineKeyboardButton("👥 User Management",callback_data="a_users")],[InlineKeyboardButton("📊 Statistics",callback_data="a_stats")],
        ])
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
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏦 Set UPI ID",callback_data="a_set_upi_id")],[InlineKeyboardButton("👤 Set UPI Name",callback_data="a_set_upi_name")],[InlineKeyboardButton("🖼 Upload QR",callback_data="a_set_qr")],[InlineKeyboardButton("⬅ Back",callback_data="a_home")]])
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
        text=self.personalize(
            settings.get("welcome_message") or "Welcome!",
            user,
            settings.get("bot_name","Subscription Bot"),
        )

        creator_line=(
            "\n\n🤖 Bot was created by "
            f'<a href="https://t.me/{MAIN_BOT_USERNAME}">'
            f"@{MAIN_BOT_USERNAME}</a>"
        )
        text=f"{text}{creator_line}"
        keyboard=self.build_welcome_keyboard(settings.get("welcome_buttons") or []) or self.main_menu()
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
        active=bool(
            sub and sub.get("active") and sub.get("expiry_date")
            and sub["expiry_date"]>now
        )

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
    async def auth(self,update,context): return update.effective_user.id==self.owner(context)

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

            record=await get_bot(owner)
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
            "🆘 Child Bot Help\n\n"
            "👤 User Commands\n"
            "/start - Open the welcome menu\n"
            "/help - Show this help guide\n"
            "/version - Check deployed bot version\n\n"
            "📋 Plans\n"
            "See all available plans and tap Buy to choose one.\n\n"
            "💳 Buy / 🔄 Renew\n"
            "Choose a plan, check UPI details, pay, then upload the payment screenshot.\n\n"
            "👤 My Profile\n"
            "See your user ID, referral details, plan, start date, expiry and remaining time.\n\n"
            "🎁 Referral\n"
            "Open your referral link and share it. Free reward days are added after a referred user completes an approved payment.\n\n"
            "📞 Support\n"
            "Send text or a photo to the seller/admin. After sending, the welcome menu opens again.\n\n"
            "⏰ Expiry\n"
            "When your plan expires, access is removed automatically. Use Renew Plan to continue."
        )

        if not is_owner:
            await update.effective_message.reply_text(user_text)
            return

        admin_text=(
            user_text
            + "\n\n━━━━━━━━━━━━━━━━━━━━\n"
            "🛠 Seller Admin Help\n\n"
            "/admin - Open the seller admin panel\n\n"
            "📦 Manage Plans\n"
            "Add: Plan Name | Duration | Price\n"
            "Example: Premium | 30d | 199\n"
            "You can view, edit, enable, disable and delete plans.\n\n"
            "📢 Channels / Groups\n"
            "Forward a message from the channel/group, or send:\n"
            "-1001234567890 | Group Name\n"
            "The child bot must be admin with invite and ban permissions.\n\n"
            "💳 Payment Settings\n"
            "Set UPI ID, UPI name and QR image.\n\n"
            "📨 Pending Payments\n"
            "Open a screenshot and Approve or Reject it. Payment ID and user details remain visible after processing.\n\n"
            "👥 User Management\n"
            "Search by User ID or @username. Give, extend or remove subscriptions, and ban/unban users.\n\n"
            "💬 Welcome Message\n"
            "Edit text, media and buttons, then use Preview.\n"
            "Button format:\n"
            "Plans - feature:plans && Buy - feature:buy\n"
            "Channel - https://t.me/example\n\n"
            "📢 Broadcast\n"
            "Send one text, photo, video, document, audio, voice, GIF, sticker or forwarded message.\n\n"
            "🎁 Referral Settings\n"
            "Set free reward days for each successful referral.\n\n"
            "📊 Statistics\n"
            "See users, plans, channels, pending payments and revenue."
        )
        await update.effective_message.reply_text(admin_text)

    async def admin(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        if not await self.auth(update,context): await update.effective_message.reply_text("❌ Not authorized"); return
        context.user_data.clear(); await update.effective_message.reply_text("🛠 Seller Admin Panel",reply_markup=self.admin_menu())

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
            record=await get_bot(owner)
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

            kb=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📤 Upload Screenshot",
                        callback_data="c_upload",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅ Back",
                        callback_data="c_buy",
                    )
                ],
            ])

            if s.get("upi_qr_file_id"):
                await q.message.reply_photo(
                    s["upi_qr_file_id"],
                    caption=text,
                    reply_markup=kb,
                )
            else:
                await self.safe_query_message(q,text,kb)
            return

        if action=="c_upload":
            context.user_data["waiting_child_screenshot"]=True
            await q.message.reply_text(
                "📷 Send payment screenshot.",
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
            context.user_data["waiting_support_message"]=True

            await self.safe_query_message(
                q,
                "📞 Send your message for admin.",
                back_keyboard,
            )
            return

        await q.answer(
            "Button action not found",
            show_alert=True,
        )

    async def admin_callback(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        q=update.callback_query; await q.answer(); owner=self.owner(context)
        if q.from_user.id!=owner: await q.edit_message_text("❌ Not authorized"); return
        a=q.data
        if a=="a_home": context.user_data.clear(); await q.edit_message_text("🛠 Seller Admin Panel",reply_markup=self.admin_menu()); return
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
            context.user_data.clear(); context.user_data["wait_channel"]=True; await q.edit_message_text("Forward a channel/group message.\nIf private group is not detected, send:\n-1001234567890 | Group Name",reply_markup=self.back("a_channels")); return
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
                reply_markup=self.channels_menu(),
            )
            return
        if a.startswith("a_channel_del_"): await remove_channel(owner,int(a.replace("a_channel_del_",""))); await q.edit_message_text("✅ Removed",reply_markup=self.channels_menu()); return
        if a=="a_welcome":
            s=await ensure_seller_defaults(owner,(await get_bot(owner) or {}).get("bot_name","Subscription Bot"))
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
            s=await ensure_seller_defaults(owner,(await get_bot(owner) or {}).get("bot_name","Subscription Bot"))
            try:
                await q.message.reply_text("👀 Preview — users will see the message below:")
                await self.send_welcome(q.message,context,s,q.from_user)
            except Exception as exc:
                logger.exception("Welcome preview failed for owner=%s",owner)
                await q.message.reply_text(f"❌ Preview failed: {str(exc)[:300]}",reply_markup=self.welcome_menu())
            return
        if a=="a_payment":
            s=await get_seller_settings(owner); await q.edit_message_text(f"💳 Payment Settings\n\nUPI Name: {s.get('upi_name') or 'Not Set'}\nUPI ID: {s.get('upi_id') or 'Not Set'}\nQR: {'Added' if s.get('upi_qr_file_id') else 'Not Added'}",reply_markup=self.payment_menu()); return
        state={"a_set_upi_id":("wait_upi_id","Send UPI ID","a_payment"),"a_set_upi_name":("wait_upi_name","Send UPI Name","a_payment"),"a_set_bot_name":("wait_bot_name","Send Bot Name","a_settings"),"a_set_support":("wait_support","Send Support Username","a_settings"),"a_set_currency":("wait_currency","Send Currency","a_settings"),"a_set_timezone":("wait_timezone","Send Timezone","a_settings"),"a_set_reminder":("wait_reminder","Send Reminder Days","a_settings"),"a_set_referral_days":("wait_referral_days","Send free reward days per successful referral","a_settings")}
        if a in state:
            key,msg,back=state[a]; context.user_data.clear(); context.user_data[key]=True; await q.edit_message_text(msg,reply_markup=self.back(back)); return
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
                )
                if referral:
                    settings=await get_seller_settings(owner)
                    reward_days=int(
                        settings.get("referral_reward_days",7) or 0
                    )
                    referrer_id=int(referral["referrer_user_id"])

                    if reward_days>0:
                        await activate_subscription(
                            owner,
                            referrer_id,
                            "Referral Reward",
                            reward_days*1440,
                            amount=0,
                            duration_text=f"{reward_days}d",
                        )
                        try:
                            await context.bot.send_message(
                                referrer_id,
                                "🎉 Referral Reward Added!\n"
                                f"You received {reward_days} free day(s).",
                            )
                        except Exception:
                            pass

                links=[]
                for ch in await get_channels(owner):
                    try:
                        inv=await context.bot.create_chat_invite_link(
                            ch["chat_id"],
                            member_limit=1,
                        )
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
                await context.bot.send_message(
                    p["user_id"],
                    "🎉 Payment approved\n"
                    f"Plan: {p['plan']}\n"
                    f"Added validity: {p.get('duration_text') or '-'}\n"
                    f"New expiry: {expiry_text}\n\n"
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
        if a=="a_broadcast": context.user_data.clear(); context.user_data["wait_broadcast"]=True; await q.edit_message_text("📢 Send any one message to broadcast.\n\nSupported: text, photo with caption, video, document, audio, voice, GIF, sticker and forwarded messages.",reply_markup=self.back()); return
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

            try:
                await context.bot.send_message(
                    user_id,
                    "🎉 Subscription activated by admin.\n"
                    f"Plan: {plan['name']}\n"
                    f"Duration: {plan['duration_text']}",
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

    async def text_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context); text=update.effective_message.text.strip()
        if update.effective_user.id==owner:
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
                try: ZoneInfo(text)
                except ZoneInfoNotFoundError: await update.effective_message.reply_text("❌ Invalid timezone"); return
                await set_seller_setting(owner,"timezone",text); context.user_data.clear(); await update.effective_message.reply_text("✅ Updated",reply_markup=self.settings_menu()); return
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
        if context.user_data.get("waiting_support_message"):
            context.user_data.clear()

            await context.bot.send_message(
                owner,
                "📩 Support message\n"
                f"User ID: {update.effective_user.id}\n"
                f"Name: {update.effective_user.full_name}\n"
                f"Username: @{update.effective_user.username if update.effective_user.username else 'Not set'}\n\n"
                f"{text}",
            )

            await update.effective_message.reply_text(
                "✅ Sent to admin"
            )

            record=await get_bot(owner)
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
            return

    async def broadcast_message_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)

        if update.effective_user.id!=owner:
            return

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

    async def welcome_media_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)
        if update.effective_user.id!=owner or not context.user_data.get("wait_welcome_media"): return
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

        if context.user_data.get("waiting_support_message"):
            context.user_data.clear()

            caption=update.effective_message.caption or ""
            user=update.effective_user
            admin_caption=(
                "📩 Support photo\n"
                f"User ID: {user.id}\n"
                f"Name: {user.full_name}\n"
                f"Username: @{user.username if user.username else 'Not set'}"
            )

            if caption:
                admin_caption+=f"\n\n{caption}"

            await context.bot.send_photo(
                chat_id=owner,
                photo=update.effective_message.photo[-1].file_id,
                caption=admin_caption,
            )

            await update.effective_message.reply_text(
                "✅ Photo sent to admin"
            )

            record=await get_bot(owner)
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
            return

        if update.effective_user.id==owner and context.user_data.get("wait_qr"):
            await set_seller_setting(owner,"upi_qr_file_id",update.effective_message.photo[-1].file_id); context.user_data.clear(); await update.effective_message.reply_text("✅ QR updated",reply_markup=self.payment_menu()); return
        if context.user_data.get("waiting_child_screenshot"):
            plan=context.user_data.get("selected_child_plan")
            if not plan: await update.effective_message.reply_text("Select a plan first"); return
            p=await create_payment(owner,update.effective_user.id,plan,update.effective_message.photo[-1].file_id); context.user_data.clear()
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

    async def forward_handler(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)
        if update.effective_user.id!=owner or not context.user_data.get("wait_channel"): return
        m=update.effective_message; chat=getattr(m,"forward_from_chat",None)
        if chat is None:
            origin=getattr(m,"forward_origin",None); chat=getattr(origin,"chat",None)
        if chat is None: await m.reply_text("❌ Could not detect. Send manually: -1001234567890 | Group Name"); return
        await add_channel(owner,chat.id,chat.title or "Unknown",getattr(chat,"type","unknown")); context.user_data.clear(); await m.reply_text("✅ Channel/group added",reply_markup=self.channels_menu())

    async def expiry_job(self,context:ContextTypes.DEFAULT_TYPE):
        owner=self.owner(context)

        for sub in await expired_subscriptions(owner):
            uid=sub["user_id"]

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

    def build_app(self,token,owner):
        app=Application.builder().token(token).build(); app.bot_data["seller_owner_id"]=owner
        app.add_handler(CommandHandler("start",self.child_start))
        app.add_handler(CommandHandler("help",self.help_command))
        app.add_handler(CommandHandler("admin",self.admin))
        app.add_handler(
            CommandHandler(
                "version",
                lambda update,context: update.effective_message.reply_text(
                    f"Runtime: {WELCOME_RUNTIME_VERSION}"
                ),
            )
        )
        app.add_handler(CallbackQueryHandler(self.child_callback,pattern=r"^c_")); app.add_handler(CallbackQueryHandler(self.admin_callback,pattern=r"^a_"))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,self.broadcast_message_handler),group=-3)
        app.add_handler(MessageHandler(filters.FORWARDED,self.forward_handler),group=-2)
        app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,self.welcome_media_handler),group=-1)
        app.add_handler(MessageHandler(filters.PHOTO,self.photo_handler),group=0)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,self.text_handler))
        if app.job_queue: app.job_queue.run_repeating(self.expiry_job,interval=300,first=60,name=f"seller_expiry_{owner}")
        return app

    async def start_bot(self,owner_id:int)->bool:
        async with self._lock:
            if owner_id in self._running:return True
            record=await get_bot(owner_id)
            if not record or not record.get("active"):return False
            token=await get_decrypted_bot_token(owner_id)
            if not token: await set_runtime_status(owner_id,"token_missing","Missing encrypted token"); return False
            app:Optional[Application]=None
            try:
                await ensure_seller_defaults(owner_id,record.get("bot_name","Subscription Bot")); app=self.build_app(token,owner_id); await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True,allowed_updates=Update.ALL_TYPES)
                self._running[owner_id]=RunningSellerBot(owner_id,int(record["bot_id"]),app); await set_runtime_status(owner_id,"running",None); return True
            except Exception as exc:
                logger.exception("Seller bot start failed owner=%s",owner_id); await set_runtime_status(owner_id,"error",str(exc)[:500])
                if app: await self._safe_shutdown(app)
                return False

    async def _safe_shutdown(self,app):
        try:
            if app.updater and app.updater.running: await app.updater.stop()
        except Exception: pass
        try:
            if app.running: await app.stop()
        except Exception: pass
        try: await app.shutdown()
        except Exception: pass
    async def stop_bot(self,owner_id:int,runtime_status="paused"):
        async with self._lock:
            r=self._running.pop(owner_id,None)
            if r: await self._safe_shutdown(r.application)
            await set_runtime_status(owner_id,runtime_status,None); return True
    async def restart_bot(self,owner_id): await self.stop_bot(owner_id,"restarting"); return await self.start_bot(owner_id)
    async def restore_active_bots(self):
        started=failed=0
        for r in await get_all_active_bots():
            if await self.start_bot(int(r["owner_id"])):started+=1
            else:failed+=1
        return {"started":started,"failed":failed}
    async def shutdown_all(self):
        for oid in list(self._running): await self.stop_bot(oid,"service_stopped")

bot_manager=SellerBotManager()
