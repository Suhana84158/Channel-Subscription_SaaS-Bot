from datetime import timezone
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from database.admins import is_admin, add_admin, remove_admin
from database.channels import add_channel, remove_channel, get_all_channels, total_channels
from database.users import (
    total_users,
    get_user,
    get_user_by_username,
    ban_user,
    unban_user,
)
from database.payments import total_revenue
from database.settings import get_setting, get_setting_value, set_setting
from database.subscriptions import (
    get_subscription,
    expire_subscription,
    activate_subscription,
    renew_subscription,
)
from database.seller_data import (
    c as seller_collection,
    USERS as SELLER_USERS,
    SUBS as SELLER_SUBS,
    get_user as get_seller_user,
    get_subscription as get_seller_subscription,
    activate_subscription as activate_seller_subscription,
)
from database.seller_bots import get_bot as get_seller_bot
from services.bot_manager import bot_manager

from services.channel_service import (
    revoke_channel_access,
    grant_channel_access,
)


IST = ZoneInfo("Asia/Kolkata")


def format_time(dt):
    if not dt:
        return "-"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 User Management", callback_data="admin_users")],
        [InlineKeyboardButton("➕ Add Channel/Group", callback_data="admin_add_channel")],
        [InlineKeyboardButton("📋 Channel List", callback_data="admin_channels")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("💳 Payment Settings", callback_data="admin_payment_settings")],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin_bot_settings")],
        [InlineKeyboardButton("📨 Pending Payments", callback_data="admin_pending_payments")],
        [InlineKeyboardButton("📜 Payment History", callback_data="admin_payment_history")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("👮 Admin Commands", callback_data="admin_commands")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")]
    ])


def user_action_keyboard(user_id: int, banned: bool):
    keyboard = [
        [
            InlineKeyboardButton(
                "🎁 Give Subscription",
                callback_data=f"user_give_sub_{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⏳ Extend Subscription",
                callback_data=f"user_extend_sub_{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Remove Subscription",
                callback_data=f"user_remove_sub_{user_id}",
            )
        ],
    ]

    if banned:
        keyboard.append([
            InlineKeyboardButton(
                "✅ Unban User",
                callback_data=f"user_unban_{user_id}",
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                "🚫 Ban User",
                callback_data=f"user_ban_{user_id}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅ Back",
            callback_data="admin_users",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


def parse_plan_time(time_text: str):
    time_text = time_text.strip().lower()

    if time_text.endswith("m"):
        return int(time_text[:-1]), "minutes"

    if time_text.endswith("h"):
        return int(time_text[:-1]) * 60, "hours"

    if time_text.endswith("d"):
        return int(time_text[:-1]) * 1440, "days"

    raise ValueError("Invalid time format")


def parse_plans(text: str):
    plans = []

    for part in text.split(","):
        duration_text, price_text = part.strip().split(":")
        duration_minutes, unit = parse_plan_time(duration_text)

        plans.append({
            "duration_text": duration_text.strip(),
            "duration_minutes": duration_minutes,
            "duration_days": max(1, duration_minutes // 1440),
            "price": int(price_text.strip()),
            "unit": unit,
        })

    return plans


async def build_user_details_text(user):
    subscription = await get_subscription(user["user_id"])

    if subscription:
        plan = subscription.get("plan", "No Plan")
        expiry = format_time(subscription.get("expiry_date"))
        sub_status = "✅ Active" if subscription.get("active") else "❌ Expired"
    else:
        plan = "No Plan"
        expiry = "-"
        sub_status = "No subscription"

    banned = bool(user.get("banned"))

    return (
        "👤 User Details\n\n"
        f"🆔 ID: {user.get('user_id')}\n"
        f"👤 Name: {user.get('first_name') or '-'}\n"
        f"📛 Username: @{user.get('username') if user.get('username') else 'None'}\n"
        f"🚫 Banned: {'Yes' if banned else 'No'}\n"
        f"📝 Reason: {user.get('ban_reason') or '-'}\n"
        f"📅 Joined: {format_time(user.get('joined_at'))}\n\n"
        f"💎 Plan: {plan}\n"
        f"📅 Expiry: {expiry}\n"
        f"📌 Status: {sub_status}"
    )


async def show_user_details(query, user):
    text = await build_user_details_text(user)
    banned = bool(user.get("banned"))

    await query.edit_message_text(
        text,
        reply_markup=user_action_keyboard(user["user_id"], banned),
    )


def seller_user_action_keyboard(owner_id:int,user_id:int,banned:bool):
    prefix=f"owner_su_{int(owner_id)}_{int(user_id)}"
    rows=[
        [InlineKeyboardButton("🎁 Give Subscription",callback_data=prefix+"_give")],
        [InlineKeyboardButton("⏳ Extend Subscription",callback_data=prefix+"_extend")],
        [InlineKeyboardButton("❌ Remove Subscription",callback_data=prefix+"_remove")],
    ]
    rows.append([InlineKeyboardButton("✅ Unban User" if banned else "🚫 Ban User",callback_data=prefix+("_unban" if banned else "_ban"))])
    rows.append([InlineKeyboardButton("⬅ Back",callback_data="admin_users")])
    return InlineKeyboardMarkup(rows)


async def build_seller_user_details_text(owner_id:int,user:dict):
    sub=await get_seller_subscription(int(owner_id),int(user["user_id"])) or {}
    bot_record=await get_seller_bot(int(owner_id)) or {}
    return (
        "👤 Clone Bot User Details\n\n"
        f"🏪 Seller ID: {owner_id}\n"
        f"🤖 Clone Bot: @{bot_record.get('bot_username','Not connected')}\n"
        f"🆔 User ID: {user.get('user_id')}\n"
        f"👤 Name: {' '.join(x for x in [user.get('first_name'),user.get('last_name')] if x) or '-'}\n"
        f"📛 Username: @{user.get('username') if user.get('username') else 'None'}\n"
        f"🚫 Banned: {'Yes' if user.get('banned') else 'No'}\n"
        f"📅 Joined: {format_time(user.get('joined_at'))}\n\n"
        f"💎 Plan: {sub.get('plan') or 'No Plan'}\n"
        f"📅 Expiry: {format_time(sub.get('expiry_date'))}\n"
        f"📌 Status: {'✅ Active' if sub.get('active') else '❌ Inactive'}"
    )


async def show_seller_user_details(query,owner_id:int,user:dict):
    await query.edit_message_text(
        await build_seller_user_details_text(owner_id,user),
        reply_markup=seller_user_action_keyboard(owner_id,user["user_id"],bool(user.get("banned"))),
    )


async def find_seller_users(search:str):
    value=search.strip()
    if value.startswith("@"):
        query={"username_normalized":value[1:].lower()}
    else:
        try:
            query={"user_id":int(value)}
        except ValueError:
            query={"username_normalized":value.lower()}
    return await seller_collection(SELLER_USERS).find(query).limit(20).to_list(length=20)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep /admin backward-compatible, but open the Owner Dashboard."""
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    from handlers.main_dashboard import owner_dashboard_keyboard, owner_dashboard_text

    await update.message.reply_text(
        await owner_dashboard_text(),
        reply_markup=owner_dashboard_keyboard(),
    )

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ You are not authorized.")
        return

    if query.data == "admin_users":
        context.user_data.clear()
        context.user_data["waiting_user_search"] = True

        await query.edit_message_text(
            "👥 User Management\n\nSend User ID or @username to search.",
            reply_markup=back_keyboard(),
        )

    elif query.data.startswith("user_ban_"):
        user_id = int(query.data.replace("user_ban_", ""))

        await ban_user(user_id, "Banned by admin")
        await revoke_channel_access(user_id)

        user = await get_user(user_id)
        await show_user_details(query, user)

    elif query.data.startswith("user_unban_"):
        user_id = int(query.data.replace("user_unban_", ""))

        await unban_user(user_id)

        user = await get_user(user_id)
        await show_user_details(query, user)

    elif query.data.startswith("user_give_sub_"):
        user_id = int(query.data.replace("user_give_sub_", ""))

        context.user_data.clear()
        context.user_data["give_sub_user"] = user_id

        await query.edit_message_text(
            "🎁 Give Subscription\n\n"
            "Send duration.\n\n"
            "Examples:\n"
            "1m\n30m\n1h\n1d\n30d\n90d\n365d",
            reply_markup=back_keyboard(),
        )

    elif query.data.startswith("user_extend_sub_"):
        user_id = int(query.data.replace("user_extend_sub_", ""))

        context.user_data.clear()
        context.user_data["extend_sub_user"] = user_id

        await query.edit_message_text(
            "⏳ Extend Subscription\n\n"
            "Send duration.\n\n"
            "Examples:\n"
            "1m\n30m\n1h\n1d\n30d\n90d\n365d",
            reply_markup=back_keyboard(),
        )

    elif query.data.startswith("user_remove_sub_"):
        user_id = int(query.data.replace("user_remove_sub_", ""))

        await expire_subscription(user_id)
        await revoke_channel_access(user_id)

        user = await get_user(user_id)
        await show_user_details(query, user)

    elif query.data.startswith("owner_su_"):
        parts=query.data.split("_")
        if len(parts)!=5:
            await query.answer("Invalid user action",show_alert=True)
            return
        seller_owner_id=int(parts[2]); user_id=int(parts[3]); action=parts[4]
        user=await get_seller_user(seller_owner_id,user_id)
        if not user:
            await query.edit_message_text("❌ Clone bot user not found.",reply_markup=back_keyboard())
            return
        if action=="view":
            await show_seller_user_details(query,seller_owner_id,user)
            return
        if action in {"give","extend"}:
            context.user_data.clear()
            context.user_data["owner_seller_sub_action"]={"owner_id":seller_owner_id,"user_id":user_id,"action":action}
            await query.edit_message_text(
                ("🎁 Give" if action=="give" else "⏳ Extend")+" Clone Bot Subscription\n\nSend duration, for example: 30m, 1h, 1d, 30d.",
                reply_markup=back_keyboard(),
            )
            return
        if action=="remove":
            await seller_collection(SELLER_SUBS).update_one({"owner_id":seller_owner_id,"user_id":user_id},{"$set":{"active":False}})
        elif action=="ban":
            await seller_collection(SELLER_USERS).update_one({"owner_id":seller_owner_id,"user_id":user_id},{"$set":{"banned":True,"ban_reason":"Banned by platform owner"}})
        elif action=="unban":
            await seller_collection(SELLER_USERS).update_one({"owner_id":seller_owner_id,"user_id":user_id},{"$set":{"banned":False,"ban_reason":""}})
        user=await get_seller_user(seller_owner_id,user_id)
        await show_seller_user_details(query,seller_owner_id,user)
        return

    elif query.data == "admin_add_channel":
        context.user_data.clear()
        context.user_data["waiting_channel"] = True

        await query.edit_message_text(
            "📢 Forward any message from your channel/group.\n\n"
            "⚠ Bot must be admin there."
        )

    elif query.data == "admin_channels":
        channels = await get_all_channels()

        if not channels:
            await query.edit_message_text(
                "📋 No channel/group added yet.",
                reply_markup=back_keyboard(),
            )
            return

        text = "📋 Added Channels/Groups:\n\n"
        keyboard = []

        for channel in channels:
            chat_id = channel.get("chat_id")
            title = channel.get("title", "Unknown")
            plans = channel.get("plans", [])

            text += f"• {title}\nID: {chat_id}\n"

            if plans:
                for plan in plans:
                    text += f"  - {plan.get('duration_text')} = ₹{plan.get('price')}\n"
            else:
                text += "  - No plans set\n"

            text += "\n"

            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Remove {title}",
                    callback_data=f"admin_remove_{chat_id}",
                )
            ])

        keyboard.append([InlineKeyboardButton("⬅ Back", callback_data="main_owner_dashboard")])

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data.startswith("admin_remove_"):
        chat_id = int(query.data.replace("admin_remove_", ""))

        await remove_channel(chat_id)

        await query.edit_message_text(
            "✅ Channel/Group removed successfully.",
            reply_markup=back_keyboard(),
        )

    elif query.data == "admin_payment_settings":
        upi = await get_setting("upi_id")
        name = await get_setting("upi_name")
        qr = await get_setting("upi_qr_file_id")

        text = (
            "💳 Payment Settings\n\n"
            f"👤 UPI Name: {name['value'] if name else 'Not Set'}\n"
            f"🏦 UPI ID: {upi['value'] if upi else 'Not Set'}\n"
            f"🖼 QR Code: {'✅ Added' if qr else '❌ Not Added'}"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏ Set UPI ID", callback_data="set_upi_id")],
            [InlineKeyboardButton("👤 Set UPI Name", callback_data="set_upi_name")],
            [InlineKeyboardButton("🖼 Upload QR", callback_data="set_upi_qr")],
            [InlineKeyboardButton("⬅ Back", callback_data="main_owner_dashboard")],
        ])

        await query.edit_message_text(text, reply_markup=keyboard)

    elif query.data == "set_upi_id":
        context.user_data.clear()
        context.user_data["waiting_upi_id"] = True

        await query.edit_message_text(
            "🏦 Send the new UPI ID.",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_upi_name":
        context.user_data.clear()
        context.user_data["waiting_upi_name"] = True

        await query.edit_message_text(
            "👤 Send the new UPI Name.",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_upi_qr":
        context.user_data.clear()
        context.user_data["waiting_upi_qr"] = True

        await query.edit_message_text(
            "🖼 Send the QR Code image.",
            reply_markup=back_keyboard(),
        )

    elif query.data == "admin_bot_settings":
        bot_name = await get_setting_value(
            "bot_name",
            "Subscription Bot",
        )
        support_username = await get_setting_value(
            "support_username",
            "Not Set",
        )
        currency = await get_setting_value(
            "currency",
            "INR",
        )
        timezone_name = await get_setting_value(
            "timezone",
            "Asia/Kolkata",
        )
        reminder_days = await get_setting_value(
            "reminder_days",
            1,
        )

        text = (
            "⚙️ Bot Settings\n\n"
            f"🤖 Bot Name: {bot_name}\n"
            f"📞 Support: {support_username or 'Not Set'}\n"
            f"💵 Currency: {currency}\n"
            f"🕒 Timezone: {timezone_name}\n"
            f"🔔 Reminder: {reminder_days} day(s)"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🤖 Bot Name",
                    callback_data="set_bot_name",
                )
            ],
            [
                InlineKeyboardButton(
                    "💬 Welcome Message",
                    callback_data="set_welcome_message",
                )
            ],
            [
                InlineKeyboardButton(
                    "📞 Support Username",
                    callback_data="set_support_username",
                )
            ],
            [
                InlineKeyboardButton(
                    "💵 Currency",
                    callback_data="set_currency",
                ),
                InlineKeyboardButton(
                    "🕒 Timezone",
                    callback_data="set_timezone",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔔 Reminder Days",
                    callback_data="set_reminder_days",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅ Back",
                    callback_data="main_owner_dashboard",
                )
            ],
        ])

        await query.edit_message_text(
            text,
            reply_markup=keyboard,
        )

    elif query.data == "set_bot_name":
        context.user_data.clear()
        context.user_data["waiting_bot_name"] = True

        await query.edit_message_text(
            "🤖 Send the new Bot Name.",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_support_username":
        context.user_data.clear()
        context.user_data["waiting_support_username"] = True

        await query.edit_message_text(
            "📞 Send the new Support Username.\n\nExample:\n@YourSupport",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_currency":
        context.user_data.clear()
        context.user_data["waiting_currency"] = True

        await query.edit_message_text(
            "💵 Send currency.\n\nExample:\nINR",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_timezone":
        context.user_data.clear()
        context.user_data["waiting_timezone"] = True

        await query.edit_message_text(
            "🕒 Send timezone.\n\nExample:\nAsia/Kolkata",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_reminder_days":
        context.user_data.clear()
        context.user_data["waiting_reminder_days"] = True

        await query.edit_message_text(
            "🔔 Send reminder days.\n\nExample:\n1",
            reply_markup=back_keyboard(),
        )

    elif query.data == "set_welcome_message":
        context.user_data.clear()
        context.user_data["waiting_welcome_message"] = True

        await query.edit_message_text(
            "💬 Send the new Welcome Message.",
            reply_markup=back_keyboard(),
        )
    elif query.data == "admin_stats":
        users = await total_users()
        channels = await total_channels()
        revenue = await total_revenue()

        await query.edit_message_text(
            f"📊 Bot Statistics\n\n"
            f"👤 Users: {users}\n"
            f"📢 Channels: {channels}\n"
            f"💰 Revenue: ₹{revenue}",
            reply_markup=back_keyboard(),
        )

    elif query.data == "admin_broadcast":
        await query.edit_message_text(
            "📢 Broadcast\n\n"
            "Use command:\n"
            "/broadcast\n\n"
            "Then send text, photo, video, document, or forwarded message.",
            reply_markup=back_keyboard(),
        )

    elif query.data == "admin_commands":
        await query.edit_message_text(
            "👮 Admin Commands\n\n"
            "/admin\n"
            "/addadmin USER_ID\n"
            "/removeadmin USER_ID\n"
            "/addchannel\n"
            "/removechannel CHAT_ID\n"
            "/stats\n"
            "/broadcast",
            reply_markup=back_keyboard(),
        )

    elif query.data in ["admin_back", "admin_home", "admin_panel"]:
        context.user_data.clear()
        from handlers.main_dashboard import owner_dashboard_keyboard, owner_dashboard_text

        await query.edit_message_text(
            await owner_dashboard_text(),
            reply_markup=owner_dashboard_keyboard(),
        )

async def receive_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    text = update.message.text.strip()

    if context.user_data.get("waiting_bot_name"):
        await set_setting("bot_name", text)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Bot Name updated successfully!\n\nNew Name: {text}",
            reply_markup=admin_keyboard(),
        )
        return

    if context.user_data.get("waiting_welcome_message"):
        await set_setting("welcome_message", text)
        context.user_data.clear()
        await update.message.reply_text(
            "✅ Welcome Message updated successfully!",
            reply_markup=admin_keyboard(),
        )
        return

    if context.user_data.get("waiting_support_username"):
        value = text if text.startswith("@") else f"@{text}"
        await set_setting("support_username", value)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Support Username updated!\n\n{value}",
            reply_markup=admin_keyboard(),
        )
        return

    if context.user_data.get("waiting_currency"):
        value = text.upper()
        await set_setting("currency", value)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Currency updated!\n\n{value}",
            reply_markup=admin_keyboard(),
        )
        return

    if context.user_data.get("waiting_timezone"):
        try:
            ZoneInfo(text)
        except Exception:
            await update.message.reply_text(
                "❌ Invalid timezone. Example: Asia/Kolkata"
            )
            return
        await set_setting("timezone", text)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Timezone updated!\n\n{text}",
            reply_markup=admin_keyboard(),
        )
        return

    if context.user_data.get("waiting_reminder_days"):
        try:
            days = int(text)
            if not 0 <= days <= 365:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Send a number from 0 to 365."
            )
            return
        await set_setting("reminder_days", days)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Reminder Days updated!\n\n{days} day(s)",
            reply_markup=admin_keyboard(),
        )
        return

    if context.user_data.get("waiting_upi_id"):
        await set_setting("upi_id", text)
        context.user_data.clear()
        await update.message.reply_text("✅ UPI ID updated successfully.")
        return

    if context.user_data.get("waiting_upi_name"):
        await set_setting("upi_name", text)
        context.user_data.clear()
        await update.message.reply_text("✅ UPI Name updated successfully.")
        return

    if context.user_data.get("owner_seller_sub_action"):
        data=context.user_data["owner_seller_sub_action"]
        try:
            duration_minutes,_=parse_plan_time(text.lower())
            owner_id=int(data["owner_id"]); user_id=int(data["user_id"])
            current=await get_seller_subscription(owner_id,user_id) or {}
            plan_name=current.get("plan") or "Owner Assigned"
            expiry=await activate_seller_subscription(owner_id,user_id,plan_name,duration_minutes,amount=0,duration_text=text.lower())
            delivery=await bot_manager.deliver_subscription_access(owner_id,user_id)
            context.user_data.clear()
            await update.message.reply_text(
                "✅ Clone bot user subscription updated successfully.\n\n"
                f"🏪 Seller ID: {owner_id}\n"
                f"👤 User ID: {user_id}\n"
                f"⏳ Duration added: {text.lower()}\n"
                f"📅 New expiry: {format_time(expiry)}\n"
                f"🔗 New invite links sent: {delivery.get('sent',0)}\n"
                f"✅ Already joined chats: {delivery.get('already_member',0)}\n"
                f"⚠️ Failed links: {delivery.get('failed',0)}",
                reply_markup=back_keyboard(),
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Could not update subscription.\n\nError: {exc}")
        return

    if context.user_data.get("give_sub_user") or context.user_data.get("extend_sub_user"):
        duration_text = text.lower()
        try:
            duration_minutes, _ = parse_plan_time(duration_text)
            duration_days = duration_minutes // 1440 if duration_minutes % 1440 == 0 else 0

            if context.user_data.get("give_sub_user"):
                user_id = context.user_data.get("give_sub_user")
                expiry = await activate_subscription(
                    user_id=user_id,
                    plan_name="Admin Gift",
                    duration_days=duration_days,
                    duration_minutes=duration_minutes,
                )
                action_text = "given"
            else:
                user_id = context.user_data.get("extend_sub_user")
                expiry = await renew_subscription(
                    user_id=user_id,
                    duration_days=duration_days,
                    duration_minutes=duration_minutes,
                )
                action_text = "extended"

            await grant_channel_access(user_id)
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Subscription {action_text} successfully!\n\n"
                f"👤 User ID: {user_id}\n"
                f"⏳ Duration: {duration_text}\n"
                f"📅 Expiry: {format_time(expiry)}"
            )
        except Exception as e:
            await update.message.reply_text(
                "❌ Invalid duration or error.\n\n"
                "Use format like: 1m, 30m, 1h, 1d, 30d\n\n"
                f"Error: {e}"
            )
        return

    if context.user_data.get("waiting_user_search"):
        search=text.strip()
        main_user=None
        if search.startswith("@"):
            main_user=await get_user_by_username(search)
        else:
            try:
                main_user=await get_user(int(search))
            except Exception:
                main_user=None
        seller_users=await find_seller_users(search)
        context.user_data["waiting_user_search"]=False

        if seller_users:
            if len(seller_users)==1:
                item=seller_users[0]
                class FakeQuery:
                    async def edit_message_text(self,text,reply_markup=None,**kwargs):
                        return await update.message.reply_text(text,reply_markup=reply_markup)
                await show_seller_user_details(FakeQuery(),int(item["owner_id"]),item)
                return
            rows=[]
            for item in seller_users:
                bot_record=await get_seller_bot(int(item["owner_id"])) or {}
                label=f"@{bot_record.get('bot_username','clone_bot')} — {item.get('user_id')}"
                rows.append([InlineKeyboardButton(label[:60],callback_data=f"owner_su_{item['owner_id']}_{item['user_id']}_view")])
            rows.append([InlineKeyboardButton("⬅ Back",callback_data="admin_users")])
            await update.message.reply_text("Multiple clone-bot user records found. Choose one:",reply_markup=InlineKeyboardMarkup(rows))
            return

        if main_user:
            details=await build_user_details_text(main_user)
            await update.message.reply_text(details,reply_markup=user_action_keyboard(main_user["user_id"],bool(main_user.get("banned"))))
            return

        await update.message.reply_text("❌ User not found in the main bot or any seller clone bot.",reply_markup=back_keyboard())
        return


async def receive_channel_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.user_data.get("waiting_channel"):
        return

    message = update.message
    chat = getattr(message, "forward_from_chat", None)

    if chat is None:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None)

    if chat is None:
        await message.reply_text(
            "❌ Channel/group detect nahi hua.\n\n"
            "Please channel/group se message forward karo."
        )
        return

    context.user_data["pending_channel"] = {
        "chat_id": chat.id,
        "title": chat.title or "Unknown",
    }

    context.user_data["waiting_channel"] = False
    context.user_data["waiting_plans"] = True

    await message.reply_text(
        f"✅ Channel detected!\n\n"
        f"Title: {chat.title}\n"
        f"ID: {chat.id}\n\n"
        "Now send plans:\n\n"
        "Example:\n"
        "5m:10, 1h:20, 1d:99\n\n"
        "m = minutes\n"
        "h = hours\n"
        "d = days"
    )


async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage:\n/removechannel CHAT_ID")
        return

    await remove_channel(int(context.args[0]))
    await update.message.reply_text("✅ Channel removed successfully.")


async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage:\n/addadmin USER_ID")
        return

    await add_admin(int(context.args[0]))
    await update.message.reply_text("✅ Admin added successfully.")


async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage:\n/removeadmin USER_ID")
        return

    await remove_admin(int(context.args[0]))
    await update.message.reply_text("✅ Admin removed successfully.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    users = await total_users()
    channels = await total_channels()
    revenue = await total_revenue()

    await update.message.reply_text(
        f"📊 Bot Statistics\n\n"
        f"👤 Users: {users}\n"
        f"📢 Channels: {channels}\n"
        f"💰 Revenue: ₹{revenue}"
    )


def admin_handlers():
    return [
        CommandHandler("admin", admin_panel),
        CommandHandler("stats", stats_command),
        CommandHandler("addadmin", add_admin_command),
        CommandHandler("removeadmin", remove_admin_command),
        CommandHandler("addchannel", add_channel_start),
        CommandHandler("removechannel", remove_channel_command),
        CallbackQueryHandler(admin_buttons, pattern=r"^(admin_|user_|owner_su_|set_upi_)"),
        MessageHandler(filters.FORWARDED, receive_channel_forward),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_text),
    ]
