import re
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from config import ADMIN_IDS
from database.admins import is_admin
from database.seller_subscriptions import (
    assign_plan_with_history, create_plan_request, create_seller_payment,
    current_plan_text, decide_seller_payment, delete_paid_plan, get_config,
    get_paid_plan, get_seller_payment, pending_seller_payments, save_paid_plan,
    seller_revenue_summary, set_subscription_suspension, subscription_history,
    update_config, usage_warning,
)


def kb(rows): return InlineKeyboardMarkup(rows)
def back(target="sub_mgmt_home"): return kb([[InlineKeyboardButton("⬅ Back", callback_data=target)]])

def main_menu():
    return kb([
        [InlineKeyboardButton("📋 Plan Manage", callback_data="sub_mgmt_plans"), InlineKeyboardButton("💳 Payment Setting", callback_data="sub_mgmt_payment")],
        [InlineKeyboardButton("🆓 Free Plan Manage", callback_data="sub_mgmt_free"), InlineKeyboardButton("💎 Paid Plan Manage", callback_data="sub_mgmt_paid")],
        [InlineKeyboardButton("🎁 Free Trial", callback_data="sub_mgmt_trial"), InlineKeyboardButton("🧾 Pending Payments", callback_data="sub_mgmt_pending")],
        [InlineKeyboardButton("👤 Assign / Suspend Seller", callback_data="sub_mgmt_seller_control")],
        [InlineKeyboardButton("📜 Subscription History", callback_data="sub_mgmt_history"), InlineKeyboardButton("💰 Seller Revenue", callback_data="sub_mgmt_revenue")],
        [InlineKeyboardButton("🏷 Branding Control", callback_data="sub_mgmt_branding")],
        [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
    ])

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); a=q.data
    if not await is_admin(q.from_user.id): await q.answer("Owner only", show_alert=True); return
    cfg=await get_config()
    if a=="sub_mgmt_home":
        await q.edit_message_text("💼 Seller Subscription Management\n\nManage plans, payments, trial, seller status, history, revenue and branding.", reply_markup=main_menu()); return
    if a=="sub_mgmt_plans":
        await q.edit_message_text("📋 Plan Manage\n\nFree and paid plan limits are dynamic. Paid plans always keep SaaS branding.", reply_markup=back()); return
    if a=="sub_mgmt_payment":
        await q.edit_message_text(f"💳 Payment Setting\n\nUPI ID: {cfg.get('payment_upi_id') or '-'}\nUPI Name: {cfg.get('payment_upi_name') or '-'}", reply_markup=kb([[InlineKeyboardButton("✏ Set UPI", callback_data="sub_mgmt_payment_edit")],[InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")]])); return
    if a=="sub_mgmt_payment_edit":
        context.user_data.clear(); context.user_data["sub_wait"]="payment"
        await q.edit_message_text("Send: UPI_ID | UPI_NAME", reply_markup=back("sub_mgmt_payment")); return
    if a=="sub_mgmt_free":
        p=cfg.get("free_plan",{})
        text=("🆓 Free Plan Manage\n\n"+f"Bots: {p.get('bot_limit',1)}\nSubscribers: {p.get('active_subscriber_limit',25)}\nChannels: {p.get('channel_limit',1)}\nPlans: {p.get('plan_limit',2)}\nAdmins: {p.get('admin_limit',1)}\nBranding: Always ON")
        await q.edit_message_text(text, reply_markup=kb([[InlineKeyboardButton("✏ Edit Limits", callback_data="sub_mgmt_free_edit")],[InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")]])); return
    if a=="sub_mgmt_free_edit":
        context.user_data.clear(); context.user_data["sub_wait"]="free"
        await q.edit_message_text("Send: Bots | Subscribers | Channels | Plans | Admins", reply_markup=back("sub_mgmt_free")); return
    if a=="sub_mgmt_paid":
        rows=[]; lines=["💎 Paid Plan Manage\n"]
        for p in cfg.get("paid_plans",[]):
            lines.append(f"• {p['name']} — ₹{p['price']:g} / {p['duration_days']}d")
            rows.append([InlineKeyboardButton(f"✏ {p['name']}",callback_data=f"sub_mgmt_paid_edit_{p['plan_id']}"),InlineKeyboardButton("🗑",callback_data=f"sub_mgmt_paid_del_{p['plan_id']}")])
        rows += [[InlineKeyboardButton("➕ Add Custom Plan",callback_data="sub_mgmt_paid_add")],[InlineKeyboardButton("⬅ Back",callback_data="sub_mgmt_home")]]
        await q.edit_message_text("\n".join(lines),reply_markup=kb(rows)); return
    if a=="sub_mgmt_paid_add" or a.startswith("sub_mgmt_paid_edit_"):
        context.user_data.clear(); context.user_data["sub_wait"]="paid_add" if a.endswith("add") else "paid_edit"
        if a.startswith("sub_mgmt_paid_edit_"): context.user_data["sub_plan_id"]=a.replace("sub_mgmt_paid_edit_","")
        await q.edit_message_text("Send: Name | Price | Days | Bots | Subscribers | Channels | Plans | Admins",reply_markup=back("sub_mgmt_paid")); return
    if a.startswith("sub_mgmt_paid_del_"):
        await delete_paid_plan(a.replace("sub_mgmt_paid_del_","")); await q.edit_message_text("✅ Paid plan deleted.",reply_markup=back("sub_mgmt_paid")); return
    if a=="sub_mgmt_trial":
        await q.edit_message_text(f"🎁 Free Trial\n\nStatus: {'Enabled' if cfg.get('trial_enabled',True) else 'Disabled'}\nDays: {cfg.get('trial_days',7)}\nPlan: {cfg.get('trial_plan_id','starter')}", reply_markup=kb([[InlineKeyboardButton("🔄 Enable/Disable",callback_data="sub_mgmt_trial_toggle")],[InlineKeyboardButton("✏ Set Days & Plan",callback_data="sub_mgmt_trial_edit")],[InlineKeyboardButton("⬅ Back",callback_data="sub_mgmt_home")]])); return
    if a=="sub_mgmt_trial_toggle":
        await update_config(trial_enabled=not cfg.get("trial_enabled",True)); await q.edit_message_text("✅ Trial status updated.",reply_markup=back("sub_mgmt_trial")); return
    if a=="sub_mgmt_trial_edit":
        context.user_data.clear(); context.user_data["sub_wait"]="trial"
        await q.edit_message_text("Send: Days | Plan_ID",reply_markup=back("sub_mgmt_trial")); return
    if a=="sub_mgmt_pending":
        items=await pending_seller_payments(); rows=[]; lines=[f"🧾 Pending Seller Payments: {len(items)}\n"]
        for x in items[:30]:
            lines.append(f"• {x['payment_id']} | Seller {x['owner_id']} | {x['plan_name']} | ₹{x['amount']:g}")
            rows.append([InlineKeyboardButton(f"✅ {x['payment_id']}",callback_data=f"subpay_ok_{x['payment_id']}"),InlineKeyboardButton("❌",callback_data=f"subpay_no_{x['payment_id']}")])
        rows.append([InlineKeyboardButton("⬅ Back",callback_data="sub_mgmt_home")])
        await q.edit_message_text("\n".join(lines),reply_markup=kb(rows)); return
    if a.startswith("subpay_ok_") or a.startswith("subpay_no_"):
        pid=a.split("_",2)[2]; status="approved" if a.startswith("subpay_ok_") else "rejected"
        pay=await decide_seller_payment(pid,status,q.from_user.id)
        if not pay: await q.answer("Already processed",show_alert=True); return
        if status=="approved": await assign_plan_with_history(pay["owner_id"],pay["plan_id"],pay["duration_days"],"payment",pay["amount"],q.from_user.id)
        try: await context.bot.send_message(pay["owner_id"],f"{'✅ Approved' if status=='approved' else '❌ Rejected'}\n\nPlan: {pay['plan_name']}\nAmount: ₹{pay['amount']:g}")
        except Exception: pass
        await q.edit_message_text(f"✅ Payment {status}.",reply_markup=back("sub_mgmt_pending")); return
    if a=="sub_mgmt_seller_control":
        context.user_data.clear(); context.user_data["sub_wait"]="seller_control"
        await q.edit_message_text("Send one command:\nASSIGN | Seller_ID | Plan_ID | Days\nSUSPEND | Seller_ID | Reason\nUNSUSPEND | Seller_ID",reply_markup=back()); return
    if a=="sub_mgmt_history":
        hist=await subscription_history(limit=25); lines=["📜 Seller Subscription History\n"]
        for h in hist: lines.append(f"• {h.get('owner_id')} | {h.get('action')} | {h.get('new_plan',h.get('target_plan_id','-'))} | {h.get('created_at').strftime('%d-%m %H:%M')}")
        await q.edit_message_text("\n".join(lines),reply_markup=back()); return
    if a=="sub_mgmt_revenue":
        r=await seller_revenue_summary(); await q.edit_message_text(f"💰 Seller Revenue\n\nTotal: ₹{r['total']:g} ({r['count']} payments)\nThis month: ₹{r['month_total']:g} ({r['month_count']} payments)",reply_markup=back()); return
    if a=="sub_mgmt_branding":
        await q.edit_message_text(f"🏷 Branding Control\n\nCurrent: {cfg.get('branding_text','Powered by Subscription SaaS Bot')}\n\nBranding remains visible on Free and every Paid plan.",reply_markup=kb([[InlineKeyboardButton("✏ Edit Branding Text",callback_data="sub_mgmt_branding_edit")],[InlineKeyboardButton("⬅ Back",callback_data="sub_mgmt_home")]])); return
    if a=="sub_mgmt_branding_edit":
        context.user_data.clear(); context.user_data["sub_wait"]="branding"
        await q.edit_message_text("Send new branding text.",reply_markup=back("sub_mgmt_branding")); return

async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode=context.user_data.get("sub_wait")
    if not mode or not await is_admin(update.effective_user.id): return
    text=(update.effective_message.text or "").strip()
    try:
        if mode=="payment":
            upi,name=[x.strip() for x in text.split("|",1)]; await update_config(payment_upi_id=upi,payment_upi_name=name)
        elif mode=="free":
            v=[int(x.strip()) for x in text.split("|")];
            if len(v)!=5: raise ValueError("Need 5 values")
            cfg=await get_config(); p=dict(cfg.get("free_plan",{})); p.update(bot_limit=v[0],active_subscriber_limit=v[1],channel_limit=v[2],plan_limit=v[3],admin_limit=v[4],branding_enabled=True); await update_config(free_plan=p)
        elif mode in {"paid_add","paid_edit"}:
            x=[z.strip() for z in text.split("|")];
            if len(x)!=8: raise ValueError("Need 8 values")
            name,price,days,bots,subs,channels,plans,admins=x; pid=context.user_data.get("sub_plan_id") or re.sub(r"[^a-z0-9]+","_",name.lower()).strip("_")
            await save_paid_plan({"plan_id":pid,"name":name,"price":float(price),"duration_days":int(days),"bot_limit":int(bots),"active_subscriber_limit":int(subs),"channel_limit":int(channels),"plan_limit":int(plans),"admin_limit":int(admins),"broadcast_enabled":True,"coupon_enabled":True,"referral_enabled":True,"analytics_enabled":True,"branding_enabled":True,"active":True})
        elif mode=="trial":
            days,pid=[x.strip() for x in text.split("|",1)]; await update_config(trial_days=max(1,int(days)),trial_plan_id=pid)
        elif mode=="branding": await update_config(branding_text=text)
        elif mode=="seller_control":
            p=[x.strip() for x in text.split("|")]; cmd=p[0].upper(); sid=int(p[1])
            if cmd=="ASSIGN": await assign_plan_with_history(sid,p[2],int(p[3]),"owner",0,update.effective_user.id)
            elif cmd=="SUSPEND": await set_subscription_suspension(sid,True,p[2] if len(p)>2 else "")
            elif cmd=="UNSUSPEND": await set_subscription_suspension(sid,False,"")
            else: raise ValueError("Unknown command")
        context.user_data.clear(); await update.effective_message.reply_text("✅ Saved.",reply_markup=main_menu())
    except Exception as e: await update.effective_message.reply_text(f"❌ Invalid format: {e}")

async def seller_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan_id=context.user_data.get("seller_payment_plan")
    if not plan_id: return
    doc=await create_seller_payment(update.effective_user.id,plan_id,update.effective_message.photo[-1].file_id,context.user_data.get("seller_request_type","upgrade"))
    context.user_data.clear()
    await update.effective_message.reply_text(f"✅ Payment submitted. ID: {doc['payment_id']}\nOwner approval pending.")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(aid,doc["file_id"],caption=f"🧾 Seller Plan Payment\nSeller: {doc['owner_id']}\nPlan: {doc['plan_name']}\nAmount: ₹{doc['amount']:g}\nID: {doc['payment_id']}",reply_markup=kb([[InlineKeyboardButton("✅ Approve",callback_data=f"subpay_ok_{doc['payment_id']}"),InlineKeyboardButton("❌ Reject",callback_data=f"subpay_no_{doc['payment_id']}")]]))
        except Exception: pass

def handlers():
    return [CallbackQueryHandler(callback,pattern=r"^(sub_mgmt_|subpay_).*"),MessageHandler(filters.PHOTO,seller_photo),MessageHandler(filters.TEXT & ~filters.COMMAND,receive)]
