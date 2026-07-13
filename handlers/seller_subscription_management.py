import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from database.admins import is_admin
from database.seller_subscriptions import (
    assign_plan, delete_paid_plan, get_config, save_paid_plan, update_config,
)


def back(target="sub_mgmt_home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=target)]])


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Plan Manage", callback_data="sub_mgmt_plans")],
        [InlineKeyboardButton("💳 Payment Setting", callback_data="sub_mgmt_payment")],
        [InlineKeyboardButton("🆓 Free Plan Manage", callback_data="sub_mgmt_free")],
        [InlineKeyboardButton("💎 Paid Plan Manage", callback_data="sub_mgmt_paid")],
        [InlineKeyboardButton("🎁 Free Trial", callback_data="sub_mgmt_trial")],
        [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
    ])


async def show_home(q):
    cfg = await get_config()
    await q.edit_message_text(
        "💼 Seller Subscription Management\n\n"
        "Seller plans, limits, payment details and free trial yahan manage karein.\n\n"
        "ℹ️ Custom branding free aur paid dono plans me rahegi; remove nahi hogi.",
        reply_markup=main_menu(),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await is_admin(q.from_user.id):
        await q.edit_message_text("❌ Owner access only.")
        return
    a = q.data
    cfg = await get_config()
    if a == "sub_mgmt_home":
        await show_home(q); return
    if a == "sub_mgmt_plans":
        free = cfg.get("free_plan", {})
        paid = cfg.get("paid_plans", [])
        lines = [
            "📋 Seller Plans Overview\n",
            f"🆓 Free: {free.get('bot_limit',1)} bot, {free.get('active_subscriber_limit',25)} active subscribers, "
            f"{free.get('channel_limit',1)} channel/group, {free.get('plan_limit',2)} plans",
            "",
            "💎 Paid Plans:",
        ]
        for p in paid:
            lines.append(f"• {p.get('name')} — ₹{p.get('price'):g}/{p.get('duration_days')}d | "
                         f"Bots {p.get('bot_limit')} | Subscribers {p.get('active_subscriber_limit')}")
        lines.append("\n🔒 Branding: Always enabled on every plan")
        await q.edit_message_text("\n".join(lines), reply_markup=back()); return
    if a == "sub_mgmt_payment":
        text = ("💳 Seller Subscription Payment Setting\n\n"
                f"UPI ID: {cfg.get('payment_upi_id') or 'Not set'}\n"
                f"UPI Name: {cfg.get('payment_upi_name') or 'Not set'}\n"
                f"QR: {'Set' if cfg.get('payment_qr_file_id') else 'Not set'}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏ Set UPI ID & Name", callback_data="sub_mgmt_payment_edit")],
            [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")],
        ])
        await q.edit_message_text(text, reply_markup=kb); return
    if a == "sub_mgmt_payment_edit":
        context.user_data.clear(); context.user_data["sub_wait"] = "payment"
        await q.edit_message_text("Send: UPI_ID | UPI Name\nExample: name@upi | My Business", reply_markup=back("sub_mgmt_payment")); return
    if a == "sub_mgmt_free":
        p = cfg.get("free_plan", {})
        text = ("🆓 Free Plan Manage\n\n"
                f"Bots: {p.get('bot_limit',1)}\nActive Subscribers: {p.get('active_subscriber_limit',25)}\n"
                f"Channels/Groups: {p.get('channel_limit',1)}\nSubscription Plans: {p.get('plan_limit',2)}\n"
                f"Admins: {p.get('admin_limit',1)}\nBroadcast: {'On' if p.get('broadcast_enabled') else 'Off'}\n"
                "Branding: Always On")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏ Edit Free Limits", callback_data="sub_mgmt_free_edit")],
            [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")],
        ])
        await q.edit_message_text(text, reply_markup=kb); return
    if a == "sub_mgmt_free_edit":
        context.user_data.clear(); context.user_data["sub_wait"] = "free"
        await q.edit_message_text("Send limits:\nBots | Active Subscribers | Channels | Plans | Admins\nExample: 1 | 25 | 1 | 2 | 1", reply_markup=back("sub_mgmt_free")); return
    if a == "sub_mgmt_paid":
        kb=[]; lines=["💎 Paid Plan Manage\n"]
        for p in cfg.get("paid_plans", []):
            lines.append(f"• {p['name']} — ₹{p['price']:g} / {p['duration_days']}d")
            kb.append([InlineKeyboardButton(f"✏ {p['name']}", callback_data=f"sub_mgmt_paid_edit_{p['plan_id']}"),
                       InlineKeyboardButton("🗑", callback_data=f"sub_mgmt_paid_del_{p['plan_id']}")])
        kb += [[InlineKeyboardButton("➕ Add Paid Plan", callback_data="sub_mgmt_paid_add")],
               [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")]]
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb)); return
    if a == "sub_mgmt_paid_add":
        context.user_data.clear(); context.user_data["sub_wait"] = "paid_add"
        await q.edit_message_text("Send:\nName | Price | Days | Bots | Subscribers | Channels | Plans | Admins\nExample: Starter | 299 | 30 | 3 | 250 | 3 | 10 | 2", reply_markup=back("sub_mgmt_paid")); return
    if a.startswith("sub_mgmt_paid_edit_"):
        context.user_data.clear(); context.user_data["sub_wait"] = "paid_edit"; context.user_data["sub_plan_id"] = a.replace("sub_mgmt_paid_edit_", "")
        await q.edit_message_text("Send new values:\nName | Price | Days | Bots | Subscribers | Channels | Plans | Admins", reply_markup=back("sub_mgmt_paid")); return
    if a.startswith("sub_mgmt_paid_del_"):
        await delete_paid_plan(a.replace("sub_mgmt_paid_del_", "")); await q.edit_message_text("✅ Paid plan deleted.", reply_markup=back("sub_mgmt_paid")); return
    if a == "sub_mgmt_trial":
        text=("🎁 Free Trial\n\n"
              f"Status: {'Enabled' if cfg.get('trial_enabled',True) else 'Disabled'}\n"
              f"Days: {cfg.get('trial_days',7)}\nTrial Plan: {cfg.get('trial_plan_id','starter')}")
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Enable/Disable", callback_data="sub_mgmt_trial_toggle")],
            [InlineKeyboardButton("✏ Set Trial Days", callback_data="sub_mgmt_trial_days")],
            [InlineKeyboardButton("🎯 Assign Plan to Seller", callback_data="sub_mgmt_assign")],
            [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")],
        ])
        await q.edit_message_text(text, reply_markup=kb); return
    if a == "sub_mgmt_trial_toggle":
        await update_config(trial_enabled=not bool(cfg.get("trial_enabled", True)))
        await q.edit_message_text("✅ Free trial setting updated.", reply_markup=back("sub_mgmt_trial")); return
    if a == "sub_mgmt_trial_days":
        context.user_data.clear(); context.user_data["sub_wait"]="trial_days"
        await q.edit_message_text("Send free trial days. Example: 7", reply_markup=back("sub_mgmt_trial")); return
    if a == "sub_mgmt_assign":
        context.user_data.clear(); context.user_data["sub_wait"]="assign"
        await q.edit_message_text("Send: Seller_ID | Plan_ID | Days\nUse plan_id: free, starter, professional, business\nExample: 123456789 | starter | 30", reply_markup=back("sub_mgmt_trial")); return


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode=context.user_data.get("sub_wait")
    if not mode or not await is_admin(update.effective_user.id): return
    text=update.effective_message.text.strip()
    try:
        if mode=="payment":
            upi,name=[x.strip() for x in text.split("|",1)]
            await update_config(payment_upi_id=upi,payment_upi_name=name)
        elif mode=="free":
            vals=[int(x.strip()) for x in text.split("|")]
            if len(vals)!=5: raise ValueError("Need 5 values")
            cfg=await get_config(); p=dict(cfg.get("free_plan",{}))
            p.update(bot_limit=vals[0],active_subscriber_limit=vals[1],channel_limit=vals[2],plan_limit=vals[3],admin_limit=vals[4],branding_enabled=True)
            await update_config(free_plan=p)
        elif mode in {"paid_add","paid_edit"}:
            parts=[x.strip() for x in text.split("|")]
            if len(parts)!=8: raise ValueError("Need 8 values")
            name,price,days,bots,subs,channels,plans,admins=parts
            pid=context.user_data.get("sub_plan_id") or re.sub(r"[^a-z0-9]+","_",name.lower()).strip("_")
            await save_paid_plan({"plan_id":pid,"name":name,"price":float(price),"duration_days":int(days),"bot_limit":int(bots),"active_subscriber_limit":int(subs),"channel_limit":int(channels),"plan_limit":int(plans),"admin_limit":int(admins),"broadcast_enabled":True,"coupon_enabled":True,"referral_enabled":True,"analytics_enabled":True,"branding_enabled":True,"active":True})
        elif mode=="trial_days":
            await update_config(trial_days=max(1,int(text)))
        elif mode=="assign":
            sid,pid,days=[x.strip() for x in text.split("|")]
            await assign_plan(int(sid),pid,int(days) if pid!="free" else None)
        context.user_data.clear()
        await update.effective_message.reply_text("✅ Seller subscription setting saved.", reply_markup=main_menu())
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Invalid format: {exc}\nPlease try again.")


def handlers():
    return [
        CallbackQueryHandler(callback, pattern=r"^sub_mgmt_"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive),
    ]
