from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from database.deleting_messages import (
    add_custom_domain,
    clear_custom_domains,
    get_deleting_message_settings,
    get_deletion_stats_summary,
    get_today_deletion_stats,
    reset_deleting_message_settings,
    set_forwarded_media_enabled,
    set_ignore_admins,
    set_ignore_owner,
    set_link_protection_enabled,
    set_module_enabled,
    toggle_section_value,
)


def mark(value: bool) -> str:
    return "✅" if value else "❌"


def home_keyboard(s):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{mark(s.get('enabled', True))} Moderation Master Switch", callback_data="dm_master")],
        [InlineKeyboardButton("🗑 Delete Commands", callback_data="dm_commands")],
        [InlineKeyboardButton("🔗 Link Protection", callback_data="dm_links")],
        [InlineKeyboardButton("📦 Forwarded Media", callback_data="dm_forwarded")],
        [InlineKeyboardButton("💥 Service Messages", callback_data="dm_service")],
        [InlineKeyboardButton("🛡 Safety Settings", callback_data="dm_safety")],
        [InlineKeyboardButton("📊 Statistics", callback_data="dm_stats")],
        [InlineKeyboardButton("♻️ Reset Settings", callback_data="dm_reset_confirm")],
        [InlineKeyboardButton("⬅ Seller Dashboard", callback_data="main_seller_dashboard")],
    ])


def commands_keyboard(s):
    c=s["delete_commands"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{mark(c['admins'])} Admin Commands",callback_data="dm_t_delete_commands_admins")],
        [InlineKeyboardButton(f"{mark(c['users'])} User Commands",callback_data="dm_t_delete_commands_users")],
        [InlineKeyboardButton("⬅ Back",callback_data="dm_home")],
    ])


def links_keyboard(s):
    x=s["link_protection"]
    rows=[
        [InlineKeyboardButton(f"{mark(x['enabled'])} Link Protection",callback_data="dm_links_master")],
        [InlineKeyboardButton(f"{mark(x['all_links'])} ALL Links",callback_data="dm_t_link_protection_all_links")],
    ]
    labels=[("telegram","Telegram"),("instagram","Instagram"),("youtube","YouTube"),("facebook","Facebook"),("x_twitter","X / Twitter"),("tiktok","TikTok"),("discord","Discord")]
    for key,label in labels:
        rows.append([InlineKeyboardButton(f"{mark(x[key])} {label}",callback_data=f"dm_t_link_protection_{key}")])
    rows += [
        [InlineKeyboardButton("➕ Add Custom Domain",callback_data="dm_domain_add")],
        [InlineKeyboardButton("🧹 Clear Custom Domains",callback_data="dm_domain_clear")],
        [InlineKeyboardButton("⬅ Back",callback_data="dm_home")],
    ]
    return InlineKeyboardMarkup(rows)


def forwarded_keyboard(s):
    x=s["forwarded_media"]
    rows=[[InlineKeyboardButton(f"{mark(x['enabled'])} Forwarded Media",callback_data="dm_forwarded_master")]]
    labels=[("photo","Photo"),("video","Video"),("animation","GIF"),("document","Document"),("audio","Audio"),("voice","Voice"),("sticker","Sticker"),("video_note","Video Note")]
    for key,label in labels:
        rows.append([InlineKeyboardButton(f"{mark(x[key])} {label}",callback_data=f"dm_t_forwarded_media_{key}")])
    rows.append([InlineKeyboardButton("⬅ Back",callback_data="dm_home")])
    return InlineKeyboardMarkup(rows)


def service_keyboard(s):
    x=s["service_messages"]
    labels=[("join","Join"),("exit","Exit"),("photos","Group Photo"),("title","Title"),("pinned","Pinned"),("topic","Topic"),("boost","Boost"),("video_chats","Video Chats"),("checklist","Checklist"),("community","Community")]
    rows=[]
    for key,label in labels:
        rows.append([InlineKeyboardButton(f"{mark(x[key])} {label}",callback_data=f"dm_t_service_messages_{key}")])
    rows.append([InlineKeyboardButton("⬅ Back",callback_data="dm_home")])
    return InlineKeyboardMarkup(rows)


def safety_keyboard(s):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{mark(s['ignore_admins'])} Ignore Group Admins",callback_data="dm_ignore_admins")],
        [InlineKeyboardButton(f"{mark(s['ignore_owner'])} Ignore Seller/Owner",callback_data="dm_ignore_owner")],
        [InlineKeyboardButton("⬅ Back",callback_data="dm_home")],
    ])


async def render(q, owner_id, page="home"):
    s=await get_deleting_message_settings(owner_id)
    if page=="home":
        await q.edit_message_text("🗑 Deleting Messages\n\nChoose what the clone bot should automatically delete from connected groups.",reply_markup=home_keyboard(s))
    elif page=="commands":
        await q.edit_message_text("🗑 Delete Commands\n\nDelete commands sent by admins and/or normal users.",reply_markup=commands_keyboard(s))
    elif page=="links":
        domains=s["link_protection"].get("custom_domains",[])
        extra="\nCustom: "+", ".join(domains) if domains else "\nCustom domains: none"
        await q.edit_message_text("🔗 Link Protection\n\nALL Links deletes links from any website. Turn it OFF to use platform-specific filters."+extra,reply_markup=links_keyboard(s))
    elif page=="forwarded":
        await q.edit_message_text("📦 Forwarded Media\n\nChoose which forwarded media types should be deleted.",reply_markup=forwarded_keyboard(s))
    elif page=="service":
        await q.edit_message_text("💥 Service Messages\n\nDelete Telegram group service messages.",reply_markup=service_keyboard(s))
    elif page=="safety":
        await q.edit_message_text("🛡 Safety Settings\n\nRecommended: keep both options enabled.",reply_markup=safety_keyboard(s))


async def deleting_messages_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    owner_id=q.from_user.id
    action=q.data

    pages={"dm_home":"home","dm_commands":"commands","dm_links":"links","dm_forwarded":"forwarded","dm_service":"service","dm_safety":"safety"}
    if action in pages:
        await render(q,owner_id,pages[action]); return

    s=await get_deleting_message_settings(owner_id)
    if action=="dm_master":
        await set_module_enabled(owner_id,not s.get("enabled",True)); await render(q,owner_id); return
    if action=="dm_links_master":
        await set_link_protection_enabled(owner_id,not s["link_protection"]["enabled"]); await render(q,owner_id,"links"); return
    if action=="dm_forwarded_master":
        await set_forwarded_media_enabled(owner_id,not s["forwarded_media"]["enabled"]); await render(q,owner_id,"forwarded"); return
    if action=="dm_ignore_admins":
        await set_ignore_admins(owner_id,not s["ignore_admins"]); await render(q,owner_id,"safety"); return
    if action=="dm_ignore_owner":
        await set_ignore_owner(owner_id,not s["ignore_owner"]); await render(q,owner_id,"safety"); return
    if action.startswith("dm_t_"):
        payload=action[5:]
        section,key=payload.rsplit("_",1)
        # section names containing underscores need explicit resolution.
        for candidate in ("delete_commands","link_protection","forwarded_media","service_messages"):
            prefix=candidate+"_"
            if payload.startswith(prefix):
                section=candidate; key=payload[len(prefix):]; break
        await toggle_section_value(owner_id,section,key)
        page={"delete_commands":"commands","link_protection":"links","forwarded_media":"forwarded","service_messages":"service"}[section]
        await render(q,owner_id,page); return
    if action=="dm_domain_add":
        context.user_data["dm_waiting_domain"]=True
        await q.edit_message_text("➕ Send one domain, for example:\nexample.com\n\nSend /cancel to stop.")
        return
    if action=="dm_domain_clear":
        await clear_custom_domains(owner_id); await render(q,owner_id,"links"); return
    if action=="dm_stats":
        today=await get_today_deletion_stats(owner_id); total=await get_deletion_stats_summary(owner_id,30)
        text=("📊 Deletion Statistics\n\nToday\n"
              f"• Commands: {today['commands_deleted']}\n• Links: {today['links_deleted']}\n"
              f"• Forwarded Media: {today['forwarded_media_deleted']}\n• Service Messages: {today['service_messages_deleted']}\n"
              f"• Total: {today['total_deleted']}\n• Failed: {today['failed_deletions']}\n\nLast 30 records\n"
              f"• Total Deleted: {total['total_deleted']}\n• Failed: {total['failed_deletions']}")
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back",callback_data="dm_home")]])); return
    if action=="dm_reset_confirm":
        await q.edit_message_text("Reset all deleting-message settings?",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Reset",callback_data="dm_reset_yes")],
            [InlineKeyboardButton("❌ Cancel",callback_data="dm_home")],
        ])); return
    if action=="dm_reset_yes":
        await reset_deleting_message_settings(owner_id); await render(q,owner_id); return


async def receive_custom_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("dm_waiting_domain"):
        return
    text=(update.effective_message.text or "").strip()
    if text.lower()=="/cancel":
        context.user_data.pop("dm_waiting_domain",None)
        await update.effective_message.reply_text("Cancelled.")
        return
    try:
        await add_custom_domain(update.effective_user.id,text)
    except ValueError as exc:
        await update.effective_message.reply_text(f"❌ {exc}\nTry again or send /cancel.")
        return
    context.user_data.pop("dm_waiting_domain",None)
    s=await get_deleting_message_settings(update.effective_user.id)
    await update.effective_message.reply_text("✅ Custom domain added.",reply_markup=links_keyboard(s))


def deleting_messages_handlers():
    return [
        CallbackQueryHandler(deleting_messages_callback,pattern=r"^dm_"),
        MessageHandler(filters.TEXT,receive_custom_domain),
    ]
