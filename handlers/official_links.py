from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from database.admins import is_admin
from database.official_links import get_official_links, set_official_link


def official_links_keyboard(back_callback=None):
    # Synchronous placeholder is intentionally not used; call build_official_links_keyboard.
    return None


async def build_official_links_keyboard(back_callback=None):
    links=await get_official_links()
    rows=[]
    if links.get("channel"):
        rows.append([InlineKeyboardButton("📢 Official Channel",url=links["channel"])])
    if links.get("group"):
        rows.append([InlineKeyboardButton("💬 Community Group",url=links["group"])])
    if links.get("support"):
        rows.append([InlineKeyboardButton("📞 Contact Support",url=links["support"])])
    if back_callback:
        rows.append([InlineKeyboardButton("⬅ Back",callback_data=back_callback)])
    return InlineKeyboardMarkup(rows) if rows else (InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back",callback_data=back_callback)]]) if back_callback else None)


def owner_settings_keyboard(links):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Set Official Channel",callback_data="official_set_channel")],
        [InlineKeyboardButton("💬 Set Community Group",callback_data="official_set_group")],
        [InlineKeyboardButton("📞 Set Support",callback_data="official_set_support")],
        [InlineKeyboardButton("👀 Preview",callback_data="official_preview")],
        [InlineKeyboardButton("🗑 Remove Channel",callback_data="official_remove_channel"),InlineKeyboardButton("🗑 Remove Group",callback_data="official_remove_group")],
        [InlineKeyboardButton("🗑 Remove Support",callback_data="official_remove_support")],
        [InlineKeyboardButton("⬅ Owner Dashboard",callback_data="main_owner_dashboard")],
    ])


def settings_text(links):
    return (
        "🌐 Official Links Settings\n\n"
        f"📢 Official Channel: {links.get('channel') or 'Not set'}\n"
        f"💬 Community Group: {links.get('group') or 'Not set'}\n"
        f"📞 Support: {links.get('support') or 'Not set'}\n\n"
        "These links are used in the main dashboard, Help & Commands, and seller subscription success messages."
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    action=q.data
    if action=="official_links_open":
        links=await get_official_links()
        if not any(links.values()):
            await q.edit_message_text("🌐 Official Links\n\nOfficial links have not been configured yet.",reply_markup=await build_official_links_keyboard("main_home"))
            return
        await q.edit_message_text("🌐 Official Links\n\nOpen an official destination below.",reply_markup=await build_official_links_keyboard("main_home"),disable_web_page_preview=True)
        return
    if not await is_admin(q.from_user.id):
        await q.answer("Owner only",show_alert=True)
        return
    if action=="official_settings":
        links=await get_official_links()
        await q.edit_message_text(settings_text(links),reply_markup=owner_settings_keyboard(links),disable_web_page_preview=True)
        return
    if action.startswith("official_set_"):
        kind=action.replace("official_set_","")
        context.user_data.clear(); context.user_data["waiting_official_link"]=kind
        label={"channel":"official channel","group":"community group","support":"support account"}[kind]
        await q.edit_message_text(f"Send the {label} @username or Telegram link.\n\nExamples:\n@example\nhttps://t.me/example",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Official Links Settings",callback_data="official_settings")]]))
        return
    if action.startswith("official_remove_"):
        kind=action.replace("official_remove_","")
        await set_official_link(kind,"")
        links=await get_official_links()
        await q.edit_message_text(f"✅ {kind.title()} link removed.\n\n"+settings_text(links),reply_markup=owner_settings_keyboard(links),disable_web_page_preview=True)
        return
    if action=="official_preview":
        links=await get_official_links()
        await q.edit_message_text("👀 Official Links Preview\n\nUsers will see the direct-open buttons below.",reply_markup=await build_official_links_keyboard("official_settings"),disable_web_page_preview=True)


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind=context.user_data.get("waiting_official_link")
    if not kind:
        return
    if not await is_admin(update.effective_user.id):
        return
    try:
        value=await set_official_link(kind,update.effective_message.text or "")
    except ValueError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return
    context.user_data.clear()
    links=await get_official_links()
    await update.effective_message.reply_text(f"✅ {kind.title()} link saved:\n{value}",reply_markup=owner_settings_keyboard(links),disable_web_page_preview=True)


def handlers():
    return [
        CallbackQueryHandler(callback,pattern=r"^official_(?:links_open|settings|set_.+|remove_.+|preview)$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND,receive_link),
    ]
