import re
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from config import ADMIN_IDS
from database.admins import is_admin
from database.sellers import get_seller, sellers_collection
from database.platform_features import reserve_payment_fingerprint, release_payment_fingerprint, audit
from handlers.official_links import build_official_links_keyboard
from database.seller_subscriptions import (
    assign_plan_with_history, extend_plan_with_history, create_plan_request, create_seller_payment,
    current_plan_text, decide_seller_payment, delete_paid_plan, get_config,
    get_paid_plan, get_seller_payment, pending_seller_payments, save_paid_plan,
    seller_revenue_summary, set_subscription_suspension, subscription_history,
    update_config, usage_warning, validate_plan_limits,
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


async def extension_confirmation(update_message, context, seller_id, plan_id, days):
    seller=await get_seller(int(seller_id)) or {}
    plan=await get_paid_plan(plan_id) or {}
    from database.seller_subscriptions import get_assignment
    from datetime import timedelta
    current=await get_assignment(int(seller_id)) or {}
    current_expiry=current.get("expiry_date")
    if current_expiry and current_expiry.tzinfo is None:
        current_expiry=current_expiry.replace(tzinfo=timezone.utc)
    base=current_expiry if current_expiry and current_expiry>datetime.now(timezone.utc) else datetime.now(timezone.utc)
    new_expiry=base+timedelta(days=int(days))
    context.user_data.clear()
    context.user_data["pending_extension"]={"seller_id":int(seller_id),"plan_id":plan_id,"days":int(days)}
    await update_message.reply_text(
        "✅ Confirm Subscription Extension\n\n"
        f"Seller: {seller.get('first_name') or seller_id}\n"
        f"Plan: {plan.get('name',plan_id)}\n"
        f"Extension: +{int(days)} Days\n"
        f"New Expiry: {new_expiry.strftime('%d %b %Y')}\n\n"
        "Do you want to continue?",
        reply_markup=kb([
            [InlineKeyboardButton("✅ Confirm Extension",callback_data="sub_mgmt_extend_confirm")],
            [InlineKeyboardButton("❌ Cancel",callback_data=f"sub_mgmt_extend_cancel_{int(seller_id)}")],
        ]),
    )

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); a=q.data
    if not await is_admin(q.from_user.id): await q.answer("Owner only", show_alert=True); return
    cfg=await get_config()
    if a=="sub_mgmt_home":
        await q.edit_message_text("💼 Seller Subscription Management\n\nManage plans, payments, trial, seller status, history, revenue and branding.", reply_markup=main_menu()); return
    if a=="sub_mgmt_plans":
        await q.edit_message_text("📋 Plan Manage\n\nFree and paid plan limits are dynamic. Paid plans always keep SaaS branding.", reply_markup=back()); return
    if a=="sub_mgmt_payment":
        qr_status="Added ✅" if cfg.get("payment_qr_file_id") else "Not Added ❌"
        await q.edit_message_text(
            f"💳 Owner Payment Settings\n\n"
            f"UPI ID: {cfg.get('payment_upi_id') or '-'}\n"
            f"UPI Name: {cfg.get('payment_upi_name') or '-'}\n"
            f"QR Code: {qr_status}",
            reply_markup=kb([
                [InlineKeyboardButton("🌐 Automatic Payment Gateways", callback_data="pgcfg_owner_home")],
                [InlineKeyboardButton("🏦 Set UPI ID", callback_data="sub_mgmt_payment_upi_id")],
                [InlineKeyboardButton("👤 Set UPI Name", callback_data="sub_mgmt_payment_upi_name")],
                [InlineKeyboardButton("🖼 Upload / Change QR", callback_data="sub_mgmt_payment_qr")],
                [InlineKeyboardButton("🗑 Remove QR", callback_data="sub_mgmt_payment_qr_remove")],
                [InlineKeyboardButton("👀 Preview Payment Details", callback_data="sub_mgmt_payment_preview")],
                [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")],
            ]),
        ); return
    if a=="sub_mgmt_payment_upi_id":
        context.user_data.clear(); context.user_data["sub_wait"]="payment_upi_id"
        await q.edit_message_text("🏦 Send Owner UPI ID", reply_markup=back("sub_mgmt_payment")); return
    if a=="sub_mgmt_payment_upi_name":
        context.user_data.clear(); context.user_data["sub_wait"]="payment_upi_name"
        await q.edit_message_text("👤 Send Owner UPI Name", reply_markup=back("sub_mgmt_payment")); return
    if a=="sub_mgmt_payment_qr":
        context.user_data.clear(); context.user_data["sub_wait"]="payment_qr"
        await q.edit_message_text("🖼 Upload the Owner payment QR image.", reply_markup=back("sub_mgmt_payment")); return
    if a=="sub_mgmt_payment_qr_remove":
        await update_config(payment_qr_file_id="")
        await q.edit_message_text("✅ Owner payment QR removed.", reply_markup=back("sub_mgmt_payment")); return
    if a=="sub_mgmt_payment_preview":
        preview=(
            "💳 Seller Plan Payment\n\n"
            f"👤 UPI Name: {cfg.get('payment_upi_name') or 'Not Set'}\n"
            f"🏦 UPI ID: {cfg.get('payment_upi_id') or 'Not Set'}\n\n"
            "Sellers will see these details while buying a plan."
        )
        preview_kb=kb([[InlineKeyboardButton("⬅ Back",callback_data="sub_mgmt_payment")]])
        if cfg.get("payment_qr_file_id"):
            await q.message.reply_photo(cfg["payment_qr_file_id"],caption=preview,reply_markup=preview_kb)
        else:
            await q.edit_message_text(preview+"\n\nQR Code: Not Added",reply_markup=preview_kb)
        return
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
        try:
            await context.bot.send_message(
                pay["owner_id"],
                f"{'✅ Approved' if status=='approved' else '❌ Rejected'}\n\nPlan: {pay['plan_name']}\nAmount: ₹{pay['amount']:g}",
                reply_markup=(await build_official_links_keyboard()) if status=="approved" else None,
            )
        except Exception:
            pass
        await q.edit_message_text(f"✅ Payment {status}.",reply_markup=back("sub_mgmt_pending")); return
    if a=="sub_mgmt_seller_control":
        context.user_data.clear(); context.user_data["sub_wait"]="seller_lookup"
        await q.edit_message_text(
            "👤 Manage Seller Subscription\n\n"
            "Send the seller's Telegram User ID or @username.",
            reply_markup=back(),
        ); return
    if a.startswith("sub_mgmt_extend_") and a.replace("sub_mgmt_extend_", "").isdigit():
        seller_id=int(a.replace("sub_mgmt_extend_", ""))
        seller=await get_seller(seller_id)
        if not seller:
            await q.edit_message_text("❌ Seller not found.", reply_markup=back("main_owner_sellers")); return
        rows=[]
        for plan in cfg.get("paid_plans", []):
            if plan.get("active", True):
                rows.append([InlineKeyboardButton(
                    f"💎 {plan.get('name','Plan')}",
                    callback_data=f"sub_mgmt_extplan_{seller_id}_{plan['plan_id']}",
                )])
        rows.append([InlineKeyboardButton("⬅ Seller Details", callback_data=f"main_seller_view_{seller_id}")])
        await q.edit_message_text(
            f"⏳ Extend Subscription\n\nSeller: {seller.get('first_name') or seller_id}\n\nSelect a plan:",
            reply_markup=kb(rows),
        ); return
    if a.startswith("sub_mgmt_extplan_"):
        raw=a.replace("sub_mgmt_extplan_", "")
        seller_id_text, plan_id=raw.split("_", 1)
        seller_id=int(seller_id_text)
        plan=await get_paid_plan(plan_id)
        if not plan:
            await q.edit_message_text("❌ Plan not found.", reply_markup=back(f"sub_mgmt_extend_{seller_id}")); return
        rows=[
            [InlineKeyboardButton("7 Days",callback_data=f"sub_mgmt_extdays_{seller_id}_{plan_id}_7"),InlineKeyboardButton("15 Days",callback_data=f"sub_mgmt_extdays_{seller_id}_{plan_id}_15")],
            [InlineKeyboardButton("30 Days",callback_data=f"sub_mgmt_extdays_{seller_id}_{plan_id}_30"),InlineKeyboardButton("90 Days",callback_data=f"sub_mgmt_extdays_{seller_id}_{plan_id}_90")],
            [InlineKeyboardButton("180 Days",callback_data=f"sub_mgmt_extdays_{seller_id}_{plan_id}_180"),InlineKeyboardButton("365 Days",callback_data=f"sub_mgmt_extdays_{seller_id}_{plan_id}_365")],
            [InlineKeyboardButton("✍ Custom Days",callback_data=f"sub_mgmt_extcustom_{seller_id}_{plan_id}")],
            [InlineKeyboardButton("⬅ Select Plan",callback_data=f"sub_mgmt_extend_{seller_id}")],
        ]
        await q.edit_message_text(
            f"⏳ Extend Subscription\n\nSelected Plan: {plan.get('name',plan_id)}\n\nHow many days of validity do you want to add?",
            reply_markup=kb(rows),
        ); return
    if a.startswith("sub_mgmt_extdays_"):
        raw=a.replace("sub_mgmt_extdays_","")
        seller_id_text,rest=raw.split("_",1)
        plan_id,days_text=rest.rsplit("_",1)
        seller=await get_seller(int(seller_id_text)) or {}
        plan=await get_paid_plan(plan_id) or {}
        from database.seller_subscriptions import get_assignment
        from datetime import timedelta
        current=await get_assignment(int(seller_id_text)) or {}
        current_expiry=current.get("expiry_date")
        if current_expiry and current_expiry.tzinfo is None:
            current_expiry=current_expiry.replace(tzinfo=timezone.utc)
        base=current_expiry if current_expiry and current_expiry>datetime.now(timezone.utc) else datetime.now(timezone.utc)
        new_expiry=base+timedelta(days=int(days_text))
        context.user_data.clear()
        context.user_data["pending_extension"]={"seller_id":int(seller_id_text),"plan_id":plan_id,"days":int(days_text)}
        await q.edit_message_text(
            "✅ Confirm Subscription Extension\n\n"
            f"Seller: {seller.get('first_name') or seller_id_text}\n"
            f"Plan: {plan.get('name',plan_id)}\n"
            f"Extension: +{int(days_text)} Days\n"
            f"New Expiry: {new_expiry.strftime('%d %b %Y')}\n\n"
            "Do you want to continue?",
            reply_markup=kb([
                [InlineKeyboardButton("✅ Confirm Extension",callback_data="sub_mgmt_extend_confirm")],
                [InlineKeyboardButton("❌ Cancel",callback_data=f"sub_mgmt_extend_cancel_{int(seller_id_text)}")],
            ]),
        )
        return
    if a.startswith("sub_mgmt_extcustom_"):
        raw=a.replace("sub_mgmt_extcustom_","")
        seller_id_text,plan_id=raw.split("_",1)
        context.user_data.clear()
        context.user_data["sub_wait"]="extend_days"
        context.user_data["extend_seller_id"]=int(seller_id_text)
        context.user_data["extend_plan_id"]=plan_id
        await q.edit_message_text(
            "✍ Custom Validity\n\nSend only the number of days.\n\nExample: 30",
            reply_markup=back(f"sub_mgmt_extplan_{seller_id_text}_{plan_id}"),
        ); return
    if a=="sub_mgmt_extend_confirm":
        pending=context.user_data.get("pending_extension")
        if not pending:
            await q.edit_message_text(
                "❌ Extension session expired. Please select the seller again.",
                reply_markup=back("main_owner_sellers"),
            ); return
        result=await extend_plan_with_history(
            pending["seller_id"], pending["plan_id"], pending["days"],
            "owner", q.from_user.id,
        )
        expiry=result.get("expiry_date")
        seller=await get_seller(pending["seller_id"]) or {}
        plan=await get_paid_plan(pending["plan_id"]) or {}
        context.user_data.clear()
        try:
            await context.bot.send_message(
                pending["seller_id"],
                "✅ Your seller subscription has been extended.\n\n"
                f"Plan: {plan.get('name',pending['plan_id'])}\n"
                f"Added Validity: {pending['days']} days\n"
                f"New Expiry: {expiry.strftime('%d %b %Y, %I:%M %p UTC') if expiry else '-'}",
                reply_markup=await build_official_links_keyboard(),
            )
        except Exception:
            pass
        await q.edit_message_text(
            "✅ Subscription Extended Successfully\n\n"
            f"Seller: {seller.get('first_name') or pending['seller_id']}\n"
            f"Plan: {plan.get('name',pending['plan_id'])}\n"
            f"Added: {pending['days']} Days\n"
            f"New Expiry: {expiry.strftime('%d %b %Y, %I:%M %p UTC') if expiry else '-'}",
            reply_markup=back(f"main_seller_view_{pending['seller_id']}"),
        ); return
    if a.startswith("sub_mgmt_extend_cancel"):
        pending=context.user_data.get("pending_extension") or {}
        seller_id=pending.get("seller_id")
        if not seller_id and a.startswith("sub_mgmt_extend_cancel_"):
            seller_id=int(a.replace("sub_mgmt_extend_cancel_",""))
        context.user_data.clear()
        await q.edit_message_text(
            "❌ Extension cancelled.",
            reply_markup=back(f"main_seller_view_{seller_id}" if seller_id else "main_owner_sellers"),
        ); return
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
        if mode=="payment_upi_id":
            if not text: raise ValueError("UPI ID required")
            await update_config(payment_upi_id=text)
        elif mode=="payment_upi_name":
            if not text: raise ValueError("UPI Name required")
            await update_config(payment_upi_name=text)
        elif mode=="free":
            raw=[x.strip() for x in text.split("|")]
            if len(raw)!=5: raise ValueError("Need 5 values")
            v=validate_plan_limits(*raw)
            cfg=await get_config(); p=dict(cfg.get("free_plan",{})); p.update(bot_limit=v[0],active_subscriber_limit=v[1],channel_limit=v[2],plan_limit=v[3],admin_limit=v[4],branding_enabled=True); await update_config(free_plan=p)
        elif mode in {"paid_add","paid_edit"}:
            x=[z.strip() for z in text.split("|")];
            if len(x)!=8: raise ValueError("Need 8 values")
            name,price,days,bots,subs,channels,plans,admins=x; pid=context.user_data.get("sub_plan_id") or re.sub(r"[^a-z0-9]+","_",name.lower()).strip("_")
            if not pid: raise ValueError("Plan name must contain letters or numbers")
            duration=max(1,int(days))
            limits=validate_plan_limits(bots,subs,channels,plans,admins)
            await save_paid_plan({"plan_id":pid,"name":name,"price":max(0,float(price)),"duration_days":duration,"bot_limit":limits[0],"active_subscriber_limit":limits[1],"channel_limit":limits[2],"plan_limit":limits[3],"admin_limit":limits[4],"broadcast_enabled":True,"coupon_enabled":True,"referral_enabled":True,"analytics_enabled":True,"branding_enabled":True,"active":True})
        elif mode=="trial":
            days,pid=[x.strip() for x in text.split("|",1)]; await update_config(trial_days=max(1,int(days)),trial_plan_id=pid)
        elif mode=="branding": await update_config(branding_text=text)
        elif mode=="seller_lookup":
            seller=None
            if text.startswith("@"):
                seller=await sellers_collection().find_one({"username": {"$regex": f"^{re.escape(text[1:])}$", "$options": "i"}})
            else:
                try:
                    seller=await get_seller(int(text))
                except ValueError:
                    seller=await sellers_collection().find_one({"username": {"$regex": f"^{re.escape(text)}$", "$options": "i"}})
            if not seller:
                raise ValueError("Seller not found")
            context.user_data.clear()
            await update.effective_message.reply_text(
                "👤 Seller Subscription\n\n"
                f"Seller: {seller.get('first_name') or '-'}\n"
                f"Username: @{seller.get('username') if seller.get('username') else 'Not set'}\n"
                f"Seller ID: {seller.get('owner_id')}\n\n"
                "Choose an action:",
                reply_markup=kb([
                    [InlineKeyboardButton("⏳ Extend Subscription", callback_data=f"sub_mgmt_extend_{seller['owner_id']}")],
                    [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home")],
                ]),
            )
            return
        elif mode=="extend_days":
            days=int(text)
            if days <= 0 or days > 3650:
                raise ValueError("Days must be between 1 and 3650")
            seller_id=int(context.user_data["extend_seller_id"])
            plan_id=context.user_data["extend_plan_id"]
            await extension_confirmation(update.effective_message,context,seller_id,plan_id,days)
            return
        context.user_data.clear(); await update.effective_message.reply_text("✅ Saved.",reply_markup=main_menu())
    except Exception as e: await update.effective_message.reply_text(f"❌ Invalid format: {e}")

async def seller_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("sub_wait")=="payment_qr" and await is_admin(update.effective_user.id):
        photo=update.effective_message.photo[-1]
        await update_config(payment_qr_file_id=photo.file_id)
        context.user_data.clear()
        await update.effective_message.reply_text(
            "✅ Owner payment QR saved.",
            reply_markup=main_menu(),
        )
        return

    plan_id=context.user_data.get("seller_payment_plan")
    if not plan_id: return
    photo=update.effective_message.photo[-1]
    fingerprint=getattr(photo,"file_unique_id","")
    reserved = await reserve_payment_fingerprint(
        "seller",
        0,
        fingerprint,
        update.effective_user.id,
    )
    if not reserved:
        context.user_data.clear()
        await update.effective_message.reply_text(
            "⚠️ This payment screenshot was already submitted. "
            "Please send a new genuine payment proof."
        )
        return

    try:
        doc=await create_seller_payment(
            update.effective_user.id,
            plan_id,
            photo.file_id,
            context.user_data.get("seller_request_type","upgrade"),
        )
    except Exception:
        await release_payment_fingerprint(
            "seller",
            0,
            fingerprint,
            update.effective_user.id,
        )
        logger.exception(
            "Seller plan payment creation failed seller_id=%s plan_id=%s",
            update.effective_user.id,
            plan_id,
        )
        await update.effective_message.reply_text(
            "❌ Payment submission failed temporarily. Please try again."
        )
        return

    await audit(
        "seller_plan_payment_submitted",
        update.effective_user.id,
        update.effective_user.id,
        {"payment_id":doc.get("payment_id")},
    )
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"✅ Payment submitted. ID: {doc['payment_id']}\n"
        "Owner approval pending."
    )
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(aid,doc["file_id"],caption=f"🧾 Seller Plan Payment\nSeller: {doc['owner_id']}\nPlan: {doc['plan_name']}\nAmount: ₹{doc['amount']:g}\nID: {doc['payment_id']}",reply_markup=kb([[InlineKeyboardButton("✅ Approve",callback_data=f"subpay_ok_{doc['payment_id']}"),InlineKeyboardButton("❌ Reject",callback_data=f"subpay_no_{doc['payment_id']}")]]))
        except Exception: pass

def handlers():
    return [CallbackQueryHandler(callback,pattern=r"^(sub_mgmt_|subpay_).*"),MessageHandler(filters.PHOTO,seller_photo),MessageHandler(filters.TEXT & ~filters.COMMAND,receive)]
