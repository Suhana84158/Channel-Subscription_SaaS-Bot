from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.admins import is_admin
from database.mongo import get_database
from database.platform_features import recent_audit, get_policy
from database.seller_bots import get_all_active_bots
from database.sellers import get_all_sellers
from services.bot_manager import bot_manager


def back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")]])


async def owner_feature_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await is_admin(q.from_user.id):
        await q.edit_message_text("❌ Owner access only.")
        return
    data = q.data
    db = get_database()

    if data == "owner_seller_management_plus":
        sellers = await get_all_sellers()
        lines = ["🏪 Owner Seller Management\n"]
        for s in sellers[:30]:
            lines.append(f"• {s.get('first_name','Seller')} | ID {s.get('user_id')} | {'Suspended' if s.get('suspended') else 'Active'}")
        lines.append("\nUse Subscription Management to assign/change plans, suspend sellers and view payment history.")
        await q.edit_message_text("\n".join(lines), reply_markup=back())
        return

    if data == "owner_backup_export":
        collections = ["sellers", "seller_bots", "seller_users", "seller_payments", "seller_subscriptions", "seller_invoices"]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["collection", "record"])
        total = 0
        for name in collections:
            async for doc in db[name].find({}):
                doc.pop("_id", None)
                writer.writerow([name, repr(doc)])
                total += 1
        raw = output.getvalue().encode("utf-8")
        await context.bot.send_document(q.message.chat_id, InputFile(io.BytesIO(raw), filename=f"saas-backup-{datetime.now():%Y%m%d-%H%M}.csv"), caption=f"✅ Export ready: {total} records")
        await q.edit_message_text("✅ Backup/export generated and sent above.", reply_markup=back())
        return

    if data == "owner_health":
        ok = True
        try:
            await db.command("ping")
        except Exception:
            ok = False
        bots = await get_all_active_bots()
        running = sum(1 for b in bots if bot_manager.is_running(int(b.get("owner_id", 0))))
        text = ("🩺 Platform Health\n\n"
                f"Database: {'🟢 Connected' if ok else '🔴 Error'}\n"
                f"Configured child bots: {len(bots)}\n"
                f"Running child bots: {running}\n"
                f"Stopped/error bots: {max(0, len(bots)-running)}\n"
                f"Checked: {datetime.now(timezone.utc):%d-%m-%Y %H:%M UTC}")
        await q.edit_message_text(text, reply_markup=back())
        return

    if data == "owner_audit":
        logs = await recent_audit(25)
        lines = ["🧾 Recent Audit Logs\n"]
        for item in logs:
            created = item.get("created_at")
            stamp = created.strftime("%d-%m %H:%M") if created else "-"
            lines.append(f"• {stamp} | {item.get('action')} | Actor {item.get('actor_id')}")
        await q.edit_message_text("\n".join(lines) if logs else "No audit logs yet.", reply_markup=back())
        return

    if data == "owner_terms_policy":
        keys = ["terms", "privacy", "refund", "support"]
        rows = []
        for key in keys:
            p = await get_policy(key)
            rows.append(f"{key.title()}:\n{p.get('text')}\n")
        await q.edit_message_text("📜 Terms & Policies\n\n" + "\n".join(rows), reply_markup=back())
        return


def handlers():
    return [CallbackQueryHandler(owner_feature_callback, pattern=r"^owner_(seller_management_plus|backup_export|health|audit|terms_policy)$")]
