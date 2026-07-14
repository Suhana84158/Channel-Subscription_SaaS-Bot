from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, InvalidToken, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from database.seller_bots import delete_bot, get_bot, save_bot, set_bot_active
from database.sellers import get_or_create_seller
from database.seller_subscriptions import effective_plan, plan_limit_warning, current_plan_text, get_config, subscription_history, create_plan_request, usage_warning, seller_access_state
from database.payment_gateways import SUPPORTED_GATEWAYS, get_gateway_config
from services.bot_manager import bot_manager


def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Upgrade Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("❌ Close", callback_data="main_seller_dashboard")],
    ])


def seller_keyboard(record=None):
    """Main seller panel shortcuts. Keep this panel small and predictable."""
    if not record:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create / Connect Child Bot", callback_data="seller_connect")],
            [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
            [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
            [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
            [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 My Bot", callback_data="seller_my_bot")],
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        [InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
    ])


def bot_control_keyboard(record):
    """Controls shown only after opening My Bot."""
    active = bool(record and record.get("active"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⏸ Pause Bot" if active else "▶️ Resume Bot",
            callback_data="seller_pause" if active else "seller_resume",
        )],
        [InlineKeyboardButton("🔄 Replace Token", callback_data="seller_replace")],
        [InlineKeyboardButton("🗑 Remove Bot", callback_data="seller_remove")],
        [InlineKeyboardButton("🌐 Child Bot Payment Gateways", callback_data="pgcfg_seller_home")],
        [InlineKeyboardButton("⬅ Seller Dashboard", callback_data="main_seller_dashboard")],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
    ])


async def safe_edit(query, text, reply_markup=None):
    """Avoid user-facing crashes for repeated taps or media-backed messages."""
    try:
        return await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        message = str(exc).lower()
        if "message is not modified" in message:
            return None
        if "there is no text in the message to edit" in message or "message can't be edited" in message:
            return await query.message.reply_text(text, reply_markup=reply_markup)
        raise


async def seller_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); owner_id=q.from_user.id; action=q.data
    record=await get_bot(owner_id)
    if action=="seller_current_plan":
        await safe_edit(
            q,
            await current_plan_text(owner_id),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
                [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
                [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
            ]),
        )
        return
    if action=="seller_upgrade_plan":
        cfg=await get_config(); plans=[p for p in cfg.get("paid_plans",[]) if p.get("active",True)]
        rows=[]; lines=["💎 Buy / Change Seller Plan", ""]
        current,_=await effective_plan(owner_id)
        for p in plans:
            lines.append(f"• {p.get('name','Plan')} — ₹{p.get('price',0):g} / {p.get('duration_days',30)} days")
            typ="upgrade" if float(p.get("price",0)) >= float(current.get("price",0)) else "downgrade"
            rows.append([InlineKeyboardButton(f"Select {p.get('name')}", callback_data=f"seller_buy_{typ}_{p.get('plan_id')}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="main_seller_dashboard")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)); return
    if action.startswith("seller_buy_"):
        _,_,request_type,plan_id=action.split("_",3)
        cfg=await get_config(); plan=next((p for p in cfg.get("paid_plans",[]) if p.get("plan_id")==plan_id),None)
        if not plan: await q.answer("Plan unavailable",show_alert=True); return
        await create_plan_request(owner_id,plan_id,request_type)
        gateway_cfg=await get_gateway_config("owner",0,decrypt=True)
        rows=[]
        for gateway in SUPPORTED_GATEWAYS:
            if (gateway_cfg.get("gateways") or {}).get(gateway,{}).get("enabled"):
                rows.append([InlineKeyboardButton(f"💳 Pay with {gateway.title()}",callback_data=f"pgsp_{gateway}_{request_type}_{plan_id}")])
        if gateway_cfg.get("manual_enabled",True):
            rows.append([InlineKeyboardButton("📤 Upload Payment Screenshot",callback_data=f"seller_manual_{request_type}_{plan_id}")])
        rows.append([InlineKeyboardButton("⬅ Back",callback_data="seller_upgrade_plan")])
        await q.edit_message_text(
            f"💳 Choose Payment Method\n\nPlan: {plan.get('name')}\nAmount: ₹{plan.get('price',0):g}\n\nAutomatic gateway payment verify hote hi plan activate hoga.",
            reply_markup=InlineKeyboardMarkup(rows),
        ); return
    if action.startswith("seller_manual_"):
        _,_,request_type,plan_id=action.split("_",3)
        cfg=await get_config(); plan=next((p for p in cfg.get("paid_plans",[]) if p.get("plan_id")==plan_id),None)
        if not plan: await q.answer("Plan unavailable",show_alert=True); return
        context.user_data.clear(); context.user_data["seller_payment_plan"]=plan_id; context.user_data["seller_request_type"]=request_type
        payment_text=(
            "💳 Seller Plan Payment\n\n"
            f"Plan: {plan.get('name')}\n"
            f"Amount: ₹{plan.get('price',0):g}\n"
            f"UPI Name: {cfg.get('payment_upi_name') or 'Not Set'}\n"
            f"UPI ID: {cfg.get('payment_upi_id') or 'Not Set'}\n\n"
            "Pay using the QR/UPI details, then upload your payment screenshot here."
        )
        payment_kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Upload Payment Screenshot",callback_data=f"seller_manual_{request_type}_{plan_id}")],
            [InlineKeyboardButton("⬅ Back",callback_data="seller_upgrade_plan")],
        ])
        if cfg.get("payment_qr_file_id"):
            await q.message.reply_photo(cfg["payment_qr_file_id"],caption=payment_text,reply_markup=payment_kb)
        else:
            await q.edit_message_text(payment_text+"\n\n⚠️ QR Code is not added yet.",reply_markup=payment_kb)
        return
    if action=="seller_plan_history":
        items=await subscription_history(owner_id,15)
        lines=["📜 Your Plan History",""]
        if not items:
            lines.append("No plan history is available yet.")
        for item in items:
            created=item.get("created_at")
            if hasattr(created,"strftime"):
                date_text=created.strftime("%d-%m-%Y")
            elif created:
                date_text=str(created)[:10]
            else:
                date_text="-"
            plan_name=(
                item.get("new_plan")
                or item.get("target_plan_id")
                or item.get("plan_id")
                or "-"
            )
            action_text=str(item.get("action") or "updated").replace("_"," ").title()
            lines.append(f"• {action_text} | {plan_name} | {date_text}")
        await safe_edit(
            q,
            "\n".join(lines),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
                [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
                [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
            ]),
        )
        return
    if action in {"seller_connect","seller_replace"}:
        if action=="seller_connect" and not record:
            plan,_=await effective_plan(owner_id)
            limit=int(plan.get("bot_limit",1))
            current=1 if await get_bot(owner_id) else 0
            if limit>=0 and current>=limit:
                await q.edit_message_text(await plan_limit_warning(owner_id), reply_markup=limit_keyboard()); return
        context.user_data.clear()
        context.user_data["waiting_seller_token"] = True
        await q.edit_message_text(
            "🤖 Create / Connect Child Bot\n\n"
            "Follow these steps:\n\n"
            "1. Open @BotFather\n"
            "2. Send /newbot\n"
            "3. Choose a bot name\n"
            "4. Choose a bot username\n"
            "5. Copy the BotFather token\n"
            "6. Return here\n"
            "7. Send your BotFather token below.\n\n"
            "🔐 Security:\n"
            "Only send a token from your own BotFather account.\n\n"
            "👇 Now send your BotFather token."
        )
        return
    if action=="seller_my_bot":
        if not record:
            await safe_edit(q,"No child bot is connected.",seller_keyboard(None))
            return
        username=str(record.get("bot_username") or "Not set").lstrip("@")
        await safe_edit(
            q,
            "🤖 My Bot\n\n"
            f"Name: {record.get('bot_name') or 'Unknown'}\n"
            f"Username: @{username}\n"
            f"Status: {'Active' if record.get('active') else 'Paused'}\n"
            f"Runtime: {record.get('runtime_status') or 'unknown'}\n"
            f"Error: {record.get('runtime_error') or '-'}",
            bot_control_keyboard(record),
        )
        return
    if action=="seller_pause" and record:
        await bot_manager.stop_bot(owner_id); await set_bot_active(owner_id,False)
    elif action=="seller_resume" and record:
        await set_bot_active(owner_id,True); await bot_manager.start_bot(owner_id)
    elif action=="seller_remove" and record:
        await bot_manager.stop_bot(owner_id,"removed"); await delete_bot(owner_id)
        await q.edit_message_text("✅ Bot removed.",reply_markup=seller_keyboard(None)); return
    record=await get_bot(owner_id)
    await safe_edit(q,"🏪 Seller Dashboard",seller_keyboard(record))


async def receive_seller_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_seller_token"): return
    token=update.effective_message.text.strip(); owner_id=update.effective_user.id
    try:
        temp=Bot(token=token); me=await temp.get_me()
        existing=await get_bot(owner_id)
        if existing: await bot_manager.stop_bot(owner_id,"replacing")
        await save_bot(owner_id,me.id,me.first_name,me.username or str(me.id),token)
        context.user_data.clear(); started=await bot_manager.start_bot(owner_id)
        await update.effective_message.reply_text(
            f"✅ Bot connected: @{me.username}\nRuntime: {'running' if started else 'failed'}",
            reply_markup=seller_keyboard(await get_bot(owner_id)),
        )
    except (InvalidToken,TelegramError) as exc:
        await update.effective_message.reply_text(f"❌ Invalid token or Telegram error: {exc}")


def seller_handlers():
    return [
        CallbackQueryHandler(seller_callback,pattern=r"^seller_(connect|replace|my_bot|pause|resume|remove|upgrade_plan|current_plan|plan_history|buy_.*|manual_.*)$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND,receive_seller_token),
    ]
