from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database.seller_bots import delete_bot, get_bot, save_bot, set_bot_active
from database.sellers import get_or_create_seller
from database.seller_subscriptions import effective_plan, plan_limit_warning, current_plan_text, get_config
from services.bot_manager import bot_manager


def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Upgrade Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("❌ Close", callback_data="main_seller_dashboard")],
    ])


def seller_keyboard(record=None):
    if not record:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create / Connect Child Bot", callback_data="seller_connect")],
            [InlineKeyboardButton("📖 Setup Guide", callback_data="main_child_setup")],
            [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
        ])
    active = bool(record.get("active"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 My Bot", callback_data="seller_my_bot")],
        [InlineKeyboardButton("⏸ Pause Bot" if active else "▶️ Resume Bot", callback_data="seller_pause" if active else "seller_resume")],
        [InlineKeyboardButton("🔄 Replace Token", callback_data="seller_replace")],
        [InlineKeyboardButton("🗑 Remove Bot", callback_data="seller_remove")],
        [InlineKeyboardButton("🏪 Seller Dashboard", callback_data="main_seller_dashboard")],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="main_home")],
    ])


async def seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_or_create_seller(update.effective_user)
    record = await get_bot(update.effective_user.id)
    text = "🏪 Seller Dashboard\n\n"
    if record:
        text += (
            f"Bot: @{record.get('bot_username','-')}\n"
            f"Status: {'✅ Active' if record.get('active') else '⏸ Paused'}\n"
            f"Runtime: {record.get('runtime_status','unknown')}"
        )
    else:
        text += "No child bot connected yet."
    await update.effective_message.reply_text(text, reply_markup=seller_keyboard(record))


async def seller_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); owner_id=q.from_user.id; action=q.data
    record=await get_bot(owner_id)
    if action=="seller_current_plan":
        await q.edit_message_text(await current_plan_text(owner_id), reply_markup=seller_keyboard(record)); return
    if action=="seller_upgrade_plan":
        cfg=await get_config()
        plans=[p for p in cfg.get("paid_plans",[]) if p.get("active",True)]
        lines=["💎 Upgrade Seller Plan", ""]
        for p in plans:
            lines.append(f"• {p.get('name','Plan')} — ₹{p.get('price',0)} / {p.get('duration_days',30)} days")
        lines += ["", "Contact the SaaS owner to activate a plan."]
        await q.edit_message_text("\n".join(lines), reply_markup=seller_keyboard(record)); return
    if action in {"seller_connect","seller_replace"}:
        if action=="seller_connect" and not record:
            plan,_=await effective_plan(owner_id)
            limit=int(plan.get("bot_limit",1))
            current=1 if await get_bot(owner_id) else 0
            if limit>=0 and current>=limit:
                await q.edit_message_text(await plan_limit_warning(owner_id), reply_markup=limit_keyboard()); return
        context.user_data.clear(); context.user_data["waiting_seller_token"]=True
        await q.edit_message_text("Send your BotFather token.")
        return
    if action=="seller_my_bot":
        if not record: await q.edit_message_text("No bot connected.",reply_markup=seller_keyboard(None)); return
        await q.edit_message_text(
            f"🤖 My Bot\n\nName: {record.get('bot_name')}\nUsername: @{record.get('bot_username')}\n"
            f"Status: {'Active' if record.get('active') else 'Paused'}\nRuntime: {record.get('runtime_status','unknown')}\n"
            f"Error: {record.get('runtime_error') or '-'}",
            reply_markup=seller_keyboard(record),
        ); return
    if action=="seller_pause" and record:
        await bot_manager.stop_bot(owner_id); await set_bot_active(owner_id,False)
    elif action=="seller_resume" and record:
        await set_bot_active(owner_id,True); await bot_manager.start_bot(owner_id)
    elif action=="seller_remove" and record:
        await bot_manager.stop_bot(owner_id,"removed"); await delete_bot(owner_id)
        await q.edit_message_text("✅ Bot removed.",reply_markup=seller_keyboard(None)); return
    record=await get_bot(owner_id)
    await q.edit_message_text("🏪 Seller Dashboard",reply_markup=seller_keyboard(record))


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
        CommandHandler("seller",seller_command),
        CallbackQueryHandler(seller_callback,pattern=r"^seller_(connect|replace|my_bot|pause|resume|remove|upgrade_plan|current_plan)$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND,receive_seller_token),
    ]
