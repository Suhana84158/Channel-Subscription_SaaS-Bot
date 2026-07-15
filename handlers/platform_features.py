from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.admins import is_admin
from database.mongo import get_database
from database.platform_features import audit, get_policy, recent_audit
from database.seller_bots import get_all_active_bots
from database.sellers import get_all_sellers
from services.bot_manager import bot_manager

_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")]]
    )


def health_keyboard(has_offline: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="owner_health_refresh")]]
    if has_offline:
        rows.append([InlineKeyboardButton("🔴 View Offline Bots", callback_data="owner_health_offline")])
    rows.append([InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")])
    return InlineKeyboardMarkup(rows)


def _format_bytes(value: int | float | None) -> str:
    if value is None:
        return "Unavailable"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return "Unavailable"


def _process_memory_bytes() -> int | None:
    """Return current process RSS on Linux/Render without extra dependencies."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _aggregate_amount(db, collection: str, match: dict[str, Any]) -> tuple[float, int]:
    try:
        rows = await db[collection].aggregate(
            [
                {"$match": match},
                {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
            ]
        ).to_list(length=1)
        if not rows:
            return 0.0, 0
        return _safe_float(rows[0].get("total")), int(rows[0].get("count", 0) or 0)
    except Exception:
        return 0.0, 0


async def _latest_record(db, collection: str, query: dict[str, Any]):
    try:
        return await db[collection].find_one(query, sort=[("created_at", -1)])
    except Exception:
        return None


async def _health_snapshot(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, list[dict]]:
    db = get_database()
    now = datetime.now(timezone.utc)

    database_ok = True
    database_latency_ms: int | None = None
    started = time.perf_counter()
    try:
        await db.command("ping")
        database_latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception:
        database_ok = False

    telegram_ok = True
    telegram_latency_ms: int | None = None
    bot_username = "Unavailable"
    started = time.perf_counter()
    try:
        me = await context.bot.get_me()
        telegram_latency_ms = int((time.perf_counter() - started) * 1000)
        bot_username = f"@{me.username}" if me.username else me.full_name
    except Exception:
        telegram_ok = False

    bots = await get_all_active_bots()
    offline_bots: list[dict] = []
    running_count = 0
    for record in bots:
        owner_id = int(record.get("owner_id", 0) or 0)
        if owner_id and bot_manager.is_running(owner_id):
            running_count += 1
        else:
            offline_bots.append(record)

    # Platform totals. Every query is isolated so one old/missing collection
    # cannot break the full health page.
    async def count(collection: str, query: dict[str, Any] | None = None) -> int:
        try:
            return int(await db[collection].count_documents(query or {}))
        except Exception:
            return 0

    total_sellers = await count("sellers")
    active_sellers = await count("sellers", {"active": True, "suspended": {"$ne": True}})
    total_users = await count("seller_users")
    active_subscribers = await count(
        "seller_subscriptions",
        {
            "status": "active",
            "$or": [
                {"expiry_date": {"$gt": now}},
                {"expires_at": {"$gt": now}},
            ],
        },
    )
    if active_subscribers == 0:
        # Some older records do not contain status but still have a future expiry.
        active_subscribers = await count(
            "seller_subscriptions",
            {"$or": [{"expiry_date": {"$gt": now}}, {"expires_at": {"$gt": now}}]},
        )

    connected_total = await count("seller_channels", {"active": True})
    channel_total = await count(
        "seller_channels",
        {"active": True, "$or": [{"type": "channel"}, {"chat_type": "channel"}]},
    )
    group_total = max(0, connected_total - channel_total)

    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    clone_today_amount, clone_today_count = await _aggregate_amount(
        db,
        "seller_payments",
        {"status": {"$in": ["approved", "paid", "captured"]}, "created_at": {"$gte": today_start}},
    )
    clone_total_amount, clone_total_count = await _aggregate_amount(
        db,
        "seller_payments",
        {"status": {"$in": ["approved", "paid", "captured"]}},
    )
    seller_plan_total, seller_plan_count = await _aggregate_amount(
        db,
        "seller_plan_payments",
        {"status": {"$in": ["approved", "paid", "captured"]}},
    )
    total_revenue = clone_total_amount + seller_plan_total
    total_success_payments = clone_total_count + seller_plan_count

    db_size: int | None = None
    try:
        stats = await db.command("dbStats")
        db_size = int(stats.get("dataSize") or stats.get("storageSize") or 0)
    except Exception:
        db_size = None

    last_backup = await _latest_record(db, "platform_audit_logs", {"action": "backup_generated"})
    last_backup_text = "Never recorded"
    if last_backup and last_backup.get("created_at"):
        last_backup_text = last_backup["created_at"].astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")

    last_error = await _latest_record(
        db,
        "logs",
        {"log_type": {"$in": ["error", "exception", "critical"]}},
    )
    if last_error:
        error_message = str(last_error.get("message") or "Unknown error").replace("\n", " ")[:90]
        error_time = last_error.get("created_at")
        error_stamp = error_time.astimezone(timezone.utc).strftime("%d-%m %H:%M") if error_time else "-"
        last_error_text = f"{error_stamp} UTC — {error_message}"
    else:
        last_error_text = "No stored errors"

    load_text = "Unavailable"
    try:
        one_minute_load = os.getloadavg()[0]
        load_text = f"{one_minute_load:.2f} load"
    except (AttributeError, OSError):
        pass

    db_line = "🟢 Connected" if database_ok else "🔴 Connection error"
    if database_latency_ms is not None:
        db_line += f" ({database_latency_ms} ms)"
    telegram_line = "🟢 Connected" if telegram_ok else "🔴 API error"
    if telegram_latency_ms is not None:
        telegram_line += f" ({telegram_latency_ms} ms)"

    text = (
        "🩺 Platform Health\n\n"
        "🌐 Core Services\n"
        f"• Database: {db_line}\n"
        f"• Telegram API: {telegram_line}\n"
        f"• Main Bot: {bot_username}\n"
        f"• Process Uptime: {_format_duration(now - _PROCESS_STARTED_AT)}\n\n"
        "🤖 Clone Bots\n"
        f"• Configured: {len(bots)}\n"
        f"• Running: 🟢 {running_count}\n"
        f"• Offline/Error: {'🔴' if offline_bots else '🟢'} {len(offline_bots)}\n\n"
        "👥 Platform Usage\n"
        f"• Sellers: {active_sellers} active / {total_sellers} total\n"
        f"• Users: {total_users}\n"
        f"• Active Subscribers: {active_subscribers}\n"
        f"• Connected Channels: {channel_total}\n"
        f"• Connected Groups: {group_total}\n\n"
        "💳 Payments\n"
        f"• Today: ₹{clone_today_amount:,.2f} ({clone_today_count} payments)\n"
        f"• Total Successful: {total_success_payments}\n"
        f"• Total Revenue: ₹{total_revenue:,.2f}\n\n"
        "🖥 Runtime\n"
        f"• Process Memory: {_format_bytes(_process_memory_bytes())}\n"
        f"• Server Load: {load_text}\n"
        f"• Database Size: {_format_bytes(db_size)}\n\n"
        "🛡 Maintenance\n"
        f"• Last Backup: {last_backup_text}\n"
        f"• Last Stored Error: {last_error_text}\n\n"
        f"🕒 Checked: {now:%d-%m-%Y %H:%M:%S UTC}"
    )
    return text, offline_bots


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
        for seller in sellers[:30]:
            lines.append(
                f"• {seller.get('first_name', 'Seller')} | ID {seller.get('user_id') or seller.get('owner_id')} | "
                f"{'Suspended' if seller.get('suspended') else 'Active'}"
            )
        lines.append("\nUse Subscription Management to assign/change plans, suspend sellers and view payment history.")
        await q.edit_message_text("\n".join(lines), reply_markup=back())
        return

    if data in {"owner_backup_export", "owner_backup_restore"}:
        collections = [
            "sellers",
            "seller_bots",
            "seller_users",
            "seller_payments",
            "seller_subscriptions",
            "seller_invoices",
        ]
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
        filename = f"saas-backup-{datetime.now():%Y%m%d-%H%M}.csv"
        await context.bot.send_document(
            q.message.chat_id,
            InputFile(io.BytesIO(raw), filename=filename),
            caption=f"✅ Export ready: {total} records",
        )
        await audit(
            "backup_generated",
            actor_id=q.from_user.id,
            details={"records": total, "filename": filename},
        )
        await q.edit_message_text(
            "✅ Backup generated and sent above. Keep this file safe for manual restore/migration.",
            reply_markup=back(),
        )
        return

    if data in {"owner_health", "owner_health_refresh"}:
        text, offline_bots = await _health_snapshot(context)
        await q.edit_message_text(text, reply_markup=health_keyboard(bool(offline_bots)))
        return

    if data == "owner_health_offline":
        bots = await get_all_active_bots()
        offline = [
            bot for bot in bots
            if not bot_manager.is_running(int(bot.get("owner_id", 0) or 0))
        ]
        if not offline:
            await q.edit_message_text(
                "🟢 All configured clone bots are currently running.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Health", callback_data="owner_health_refresh")],
                    [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
                ]),
            )
            return

        lines = ["🔴 Offline / Error Clone Bots\n"]
        for record in offline[:40]:
            username = record.get("bot_username") or record.get("bot_name") or "Unknown bot"
            owner_id = record.get("owner_id", "-")
            status = record.get("runtime_status") or record.get("status") or "stopped"
            error = str(record.get("runtime_error") or "No stored error").replace("\n", " ")[:100]
            lines.append(f"• @{str(username).lstrip('@')}\n  Seller: {owner_id} | Status: {status}\n  Error: {error}")
        if len(offline) > 40:
            lines.append(f"\n…and {len(offline) - 40} more bots.")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Health", callback_data="owner_health_refresh")],
                [InlineKeyboardButton("⬅ Owner Dashboard", callback_data="main_owner_dashboard")],
            ]),
        )
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
            policy = await get_policy(key)
            rows.append(f"{key.title()}:\n{policy.get('text')}\n")
        await q.edit_message_text("📜 Terms & Policies\n\n" + "\n".join(rows), reply_markup=back())
        return


def handlers():
    return [
        CallbackQueryHandler(
            owner_feature_callback,
            pattern=r"^owner_(seller_management_plus|backup_export|backup_restore|health|health_refresh|health_offline|audit|terms_policy)$",
        )
    ]
