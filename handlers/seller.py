from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from database.seller_bots import (
    count_owner_bots,
    delete_bot,
    get_bot,
    get_bot_by_bot_id,
    get_bots,
    save_bot,
    set_bot_active,
)
from database.seller_subscriptions import (
    create_plan_request,
    current_plan_text,
    effective_plan,
    get_config,
    plan_limit_warning,
    subscription_history,
)
from services.bot_manager import bot_manager


def main_seller_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Manage My Clone Bots", callback_data="seller_bots_list")],
        [InlineKeyboardButton("➕ Create New Clone Bot", callback_data="seller_connect")],
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("📜 Plan History", callback_data="seller_plan_history")],
        [InlineKeyboardButton("🌐 Official Links", callback_data="official_links_open")],
    ])


def limit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("📊 View Current Plan", callback_data="seller_current_plan")],
        [InlineKeyboardButton("⬅ Back", callback_data="seller_bots_list")],
    ])


def seller_plan_page_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy / Change Plan", callback_data="seller_upgrade_plan")],
        [InlineKeyboardButton("⬅ Back", callback_data="main_home")],
    ])


async def clone_list_markup(owner_id: int):
    bots = await get_bots(owner_id)
    rows = []
    for record in bots:
        status = "🟢" if record.get("runtime_status") == "running" else "🔴"
        username = record.get("bot_username") or str(record.get("bot_id"))
        rows.append([InlineKeyboardButton(
            f"{status} @{username}",
            callback_data=f"seller_select_{record['bot_id']}",
        )])
    # Keep this button visible even when the seller has reached the bot limit.
    # Clicking it then opens the plan-limit warning with upgrade options.
    rows.append([InlineKeyboardButton("➕ Create New Clone Bot", callback_data="seller_connect")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main_home")])
    return InlineKeyboardMarkup(rows)


def selected_bot_markup(record):
    username = record.get("bot_username") or str(record["bot_id"])
    bot_url = f"https://t.me/{username}"
    bot_id = int(record["bot_id"])
    active = bool(record.get("active"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Seller Profile", callback_data="main_seller_profile")],
        [InlineKeyboardButton("🤖 My Bot", callback_data=f"seller_my_bot_{bot_id}")],
        [InlineKeyboardButton("💳 Payment Settings", url=f"{bot_url}?start=admin_payment")],
        [InlineKeyboardButton("⚙️ Bot Settings", url=f"{bot_url}?start=admin_settings")],
        [InlineKeyboardButton("📊 Statistics", url=f"{bot_url}?start=admin_stats")],
        [InlineKeyboardButton("📢 Channels / Groups", url=f"{bot_url}?start=admin_channels")],
        [InlineKeyboardButton("🤝 Seller Referral", callback_data="main_seller_referral")],
        [InlineKeyboardButton("📜 Terms & Policy", url=f"{bot_url}?start=admin_terms")],
        [InlineKeyboardButton(
            "⏸ Pause Bot" if active else "▶️ Resume Bot",
            callback_data=f"seller_{'pause' if active else 'resume'}_{bot_id}",
        )],
        [InlineKeyboardButton("🔄 Replace Token", callback_data=f"seller_replace_{bot_id}")],
        [InlineKeyboardButton("🗑 Remove Bot", callback_data=f"seller_remove_{bot_id}")],
        [InlineKeyboardButton("⬅ Clone Bot List", callback_data="seller_bots_list")],
    ])


async def seller_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    owner_id = int(q.from_user.id)
    action = q.data

    if action == "seller_bots_list":
        bots = await get_bots(owner_id)
        plan, _ = await effective_plan(owner_id)
        limit = int(plan.get("bot_limit", 1))
        limit_text = "Unlimited" if limit < 0 else str(limit)
        await q.edit_message_text(
            f"🤖 My Clone Bots — {len(bots)}/{limit_text}\n\nSelect a clone bot to manage.",
            reply_markup=await clone_list_markup(owner_id),
        )
        return

    if action.startswith("seller_select_"):
        bot_id = int(action.rsplit("_", 1)[1])
        record = await get_bot_by_bot_id(bot_id)
        if not record or int(record.get("owner_id", 0)) != owner_id:
            await q.answer("Clone bot not found.", show_alert=True)
            return
        context.user_data["selected_clone_bot_id"] = bot_id
        await q.edit_message_text(
            f"🤖 @{record.get('bot_username')}\n\n"
            f"Status: {'🟢 Active' if record.get('active') else '⏸ Paused'}\n"
            f"Runtime: {record.get('runtime_status', 'unknown')}\n\n"
            "Manage the selected clone bot below.",
            reply_markup=selected_bot_markup(record),
        )
        return

    if action == "seller_current_plan":
        await q.edit_message_text(await current_plan_text(owner_id), reply_markup=seller_plan_page_keyboard())
        return

    if action == "seller_upgrade_plan":
        cfg = await get_config()
        plans = [p for p in cfg.get("paid_plans", []) if p.get("active", True)]
        rows = []
        lines = ["💎 Buy / Change Seller Plan", ""]
        current, _ = await effective_plan(owner_id)
        for p in plans:
            lines.append(f"• {p.get('name','Plan')} — ₹{p.get('price',0):g} / {p.get('duration_days',30)} days")
            typ = "upgrade" if float(p.get("price", 0)) >= float(current.get("price", 0)) else "downgrade"
            rows.append([InlineKeyboardButton(f"Select {p.get('name')}", callback_data=f"seller_buy_{typ}_{p.get('plan_id')}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="main_home")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        return

    if action.startswith("seller_buy_"):
        _, _, request_type, plan_id = action.split("_", 3)
        cfg = await get_config()
        plan = next((p for p in cfg.get("paid_plans", []) if p.get("plan_id") == plan_id), None)
        if not plan:
            await q.answer("Plan unavailable", show_alert=True)
            return
        await create_plan_request(owner_id, plan_id, request_type)
        context.user_data.clear()
        context.user_data["seller_payment_plan"] = plan_id
        context.user_data["seller_request_type"] = request_type
        text = (
            "💳 Seller Plan Payment\n\n"
            f"Plan: {plan.get('name')}\nAmount: ₹{plan.get('price',0):g}\n"
            f"UPI Name: {cfg.get('payment_upi_name') or 'Not Set'}\n"
            f"UPI ID: {cfg.get('payment_upi_id') or 'Not Set'}\n\n"
            "Pay and upload your payment screenshot."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Upload Payment Screenshot", callback_data=f"seller_manual_{request_type}_{plan_id}")],
            [InlineKeyboardButton("⬅ Back", callback_data="seller_upgrade_plan")],
        ])
        if cfg.get("payment_qr_file_id"):
            await q.message.reply_photo(cfg["payment_qr_file_id"], caption=text, reply_markup=kb)
        else:
            await q.edit_message_text(text, reply_markup=kb)
        return

    if action.startswith("seller_manual_"):
        _, _, request_type, plan_id = action.split("_", 3)
        context.user_data.clear()
        context.user_data["seller_payment_plan"] = plan_id
        context.user_data["seller_request_type"] = request_type
        await q.message.reply_text("📷 Please upload your payment screenshot now.")
        return

    if action == "seller_plan_history":
        items = await subscription_history(owner_id, 15)
        lines = ["📜 Your Plan History", ""]
        if not items:
            lines.append("No plan history is available yet.")
        else:
            for item in items:
                created_at = item.get("created_at")
                date_text = created_at.strftime("%d-%m-%Y") if hasattr(created_at, "strftime") else "-"
                lines.append(
                    f"• {str(item.get('action') or 'Updated').replace('_',' ').title()}\n"
                    f"  Plan: {item.get('new_plan') or item.get('target_plan_id') or item.get('plan_name') or '-'}\n"
                    f"  Date: {date_text}"
                )
        await q.edit_message_text("\n\n".join(lines), reply_markup=seller_plan_page_keyboard())
        return

    if action == "seller_connect" or action.startswith("seller_replace_"):
        replacing_bot_id = int(action.rsplit("_", 1)[1]) if action.startswith("seller_replace_") else None
        if replacing_bot_id is None:
            plan, _ = await effective_plan(owner_id)
            limit = int(plan.get("bot_limit", 1))
            current = await count_owner_bots(owner_id)
            if limit >= 0 and current >= limit:
                await q.edit_message_text(await plan_limit_warning(owner_id), reply_markup=limit_keyboard())
                return
        else:
            record = await get_bot_by_bot_id(replacing_bot_id)
            if not record or int(record.get("owner_id", 0)) != owner_id:
                await q.answer("Clone bot not found.", show_alert=True)
                return
        context.user_data.clear()
        context.user_data["waiting_seller_token"] = True
        context.user_data["replace_clone_bot_id"] = replacing_bot_id
        await q.edit_message_text(
            "🤖 Create / Connect Clone Bot\n\n"
            "1. Open @BotFather\n2. Send /newbot\n3. Create the bot\n"
            "4. Copy its token\n5. Send the token here.\n\n"
            "🔐 Only send a token from your own BotFather account."
        )
        return

    for prefix in ("seller_my_bot_", "seller_pause_", "seller_resume_", "seller_remove_"):
        if action.startswith(prefix):
            bot_id = int(action.rsplit("_", 1)[1])
            record = await get_bot_by_bot_id(bot_id)
            if not record or int(record.get("owner_id", 0)) != owner_id:
                await q.answer("Clone bot not found.", show_alert=True)
                return
            if prefix == "seller_my_bot_":
                await q.edit_message_text(
                    f"🤖 My Bot\n\nName: {record.get('bot_name')}\n"
                    f"Username: @{record.get('bot_username')}\n"
                    f"Status: {'Active' if record.get('active') else 'Paused'}\n"
                    f"Runtime: {record.get('runtime_status','unknown')}\n"
                    f"Error: {record.get('runtime_error') or '-'}",
                    reply_markup=selected_bot_markup(record),
                )
            elif prefix == "seller_pause_":
                await bot_manager.stop_bot(bot_id)
                await set_bot_active(bot_id, False)
                record = await get_bot_by_bot_id(bot_id)
                await q.edit_message_text("⏸ Clone bot paused.", reply_markup=selected_bot_markup(record))
            elif prefix == "seller_resume_":
                await set_bot_active(bot_id, True)
                await bot_manager.start_bot(bot_id)
                record = await get_bot_by_bot_id(bot_id)
                await q.edit_message_text("▶️ Clone bot resumed.", reply_markup=selected_bot_markup(record))
            else:
                await bot_manager.stop_bot(bot_id, "removed")
                await delete_bot(owner_id, bot_id)
                await q.edit_message_text("✅ Clone bot removed.", reply_markup=await clone_list_markup(owner_id))
            return


async def receive_seller_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_seller_token"):
        return
    token = update.effective_message.text.strip()
    owner_id = int(update.effective_user.id)
    replace_bot_id = context.user_data.get("replace_clone_bot_id")
    try:
        temp = Bot(token=token)
        me = await temp.get_me()
        existing_token_record = await get_bot_by_bot_id(me.id)
        if existing_token_record and int(existing_token_record.get("owner_id", 0)) != owner_id:
            await update.effective_message.reply_text("❌ This bot is already connected to another seller.")
            return
        if replace_bot_id and int(replace_bot_id) != int(me.id):
            await bot_manager.stop_bot(int(replace_bot_id), "replacing")
            await delete_bot(owner_id, int(replace_bot_id))
        await save_bot(owner_id, me.id, me.first_name, me.username or str(me.id), token)
        context.user_data.clear()
        started = await bot_manager.start_bot(me.id)
        record = await get_bot_by_bot_id(me.id)
        await update.effective_message.reply_text(
            f"✅ Clone bot connected: @{me.username}\nRuntime: {'running' if started else 'failed'}",
            reply_markup=selected_bot_markup(record),
        )
    except (InvalidToken, TelegramError) as exc:
        await update.effective_message.reply_text(f"❌ Invalid token or Telegram error: {exc}")


def seller_handlers():
    return [
        CallbackQueryHandler(seller_callback, pattern=r"^seller_(bots_list|select_\d+|connect|replace_\d+|my_bot_\d+|pause_\d+|resume_\d+|remove_\d+|upgrade_plan|current_plan|plan_history|buy_.*|manual_.*)$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_seller_token),
    ]
