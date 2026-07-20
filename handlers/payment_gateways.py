from __future__ import annotations

from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from config import PUBLIC_BASE_URL
from database.admins import is_admin
from database.official_links import get_official_links
from database.payment_gateways import (
    SUPPORTED_GATEWAYS,
    create_gateway_transaction,
    gateway_history,
    gateway_is_ready,
    gateway_missing_fields,
    get_gateway_config,
    save_gateway_config,
)
from database.seller_subscriptions import get_paid_plan
from services.payment_gateways import GatewayError, create_checkout, test_gateway_connection


def _kb(rows):
    return InlineKeyboardMarkup(rows)


def _scope_owner(update: Update, scope: str) -> int:
    return 0 if scope == "owner" else int(update.effective_user.id)


def _status_icon(value: bool) -> str:
    return "✅" if value else "❌"


def _masked(value: str | None) -> str:
    value = str(value or "").strip()
    if not value:
        return "Not added"
    if len(value) <= 8:
        return "Added"
    return f"Added (…{value[-4:]})"


def _webhook_url(scope: str, owner_id: int) -> str:
    if not PUBLIC_BASE_URL:
        return "PUBLIC_BASE_URL is not configured"
    return f"{PUBLIC_BASE_URL}/webhooks/razorpay/{scope}/{int(owner_id)}"


def _payment_header(scope: str, cfg: dict) -> str:
    gateways = cfg.get("gateways") or {}
    rz = gateways.get("razorpay") or {}
    cf = gateways.get("cashfree") or {}
    return (
        f"💳 {'Owner' if scope == 'owner' else 'Seller'} Payment Settings\n\n"
        f"{_status_icon(bool(rz.get('enabled')))} Razorpay: {'Enabled' if rz.get('enabled') else 'Disabled'}\n"
        f"   Key ID: {_masked(rz.get('key_id'))}\n"
        f"   Key Secret: {'Added' if rz.get('key_secret') else 'Not added'}\n"
        f"{_status_icon(bool(cf.get('enabled')))} Cashfree: {'Enabled' if cf.get('enabled') else 'Disabled'}\n"
        f"   Client ID: {_masked(cf.get('client_id'))}\n"
        f"   Client Secret: {'Added' if cf.get('client_secret') else 'Not added'}\n\n"
        "Automatic gateways always use LIVE mode."
    )


def _home_keyboard(scope: str, cfg: dict):
    gateways = cfg.get("gateways") or {}
    rows = []
    for name, title in (("razorpay", "Razorpay"), ("cashfree", "Cashfree")):
        enabled = bool((gateways.get(name) or {}).get("enabled"))
        rows.append([InlineKeyboardButton(
            f"{'✅' if enabled else '❌'} {title}",
            callback_data=f"pgcfg_{scope}_{name}",
        )])
    rows += [
        [InlineKeyboardButton("📜 Gateway History", callback_data=f"pgcfg_{scope}_history")],
        [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_payment" if scope == "owner" else "main_seller_dashboard")],
    ]
    return _kb(rows)


def _gateway_keyboard(scope: str, gateway: str, enabled: bool):
    rows = [[InlineKeyboardButton(
        "⛔ Disable" if enabled else "✅ Enable",
        callback_data=f"pgcfg_{scope}_{gateway}_toggle",
    )]]
    if gateway == "razorpay":
        rows += [
            [InlineKeyboardButton("🔑 Set / Replace Key ID", callback_data=f"pgcfg_{scope}_razorpay_field_key_id")],
            [InlineKeyboardButton("🔒 Set / Replace Key Secret", callback_data=f"pgcfg_{scope}_razorpay_field_key_secret")],
            [InlineKeyboardButton("🔗 Webhook Setup", callback_data=f"pgcfg_{scope}_razorpay_webhook")],
            [InlineKeyboardButton("🧪 Test Connection", callback_data=f"pgcfg_{scope}_razorpay_test")],
        ]
    else:
        rows += [
            [InlineKeyboardButton("🔐 Set / Replace Credentials", callback_data=f"pgcfg_{scope}_{gateway}_credentials")],
            [InlineKeyboardButton("🧪 Test Connection", callback_data=f"pgcfg_{scope}_{gateway}_test")],
        ]
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_home")])
    return _kb(rows)


def _gateway_header(scope: str, gateway: str, gcfg: dict, owner_id: int) -> str:
    if gateway == "razorpay":
        credential_lines = (
            f"Key ID: {_masked(gcfg.get('key_id'))}\n"
            f"Key Secret: {'Added' if gcfg.get('key_secret') else 'Not added'}\n"
            f"Webhook URL: {'Generated ✅' if PUBLIC_BASE_URL else 'Not available ❌'}\n"
            f"Webhook Secret: {'Added ✅' if gcfg.get('webhook_secret') else 'Not added ❌'}"
        )
    else:
        credential_lines = (
            f"Client ID: {_masked(gcfg.get('client_id'))}\n"
            f"Client Secret: {'Added' if gcfg.get('client_secret') else 'Not added'}"
        )
    return (
        f"💳 {gateway.title()} — {'Owner' if scope == 'owner' else 'Seller'}\n\n"
        f"Status: {'Enabled ✅' if gcfg.get('enabled') else 'Disabled ❌'}\n"
        "Mode: LIVE 🚀\n"
        f"{credential_lines}"
    )


def _webhook_setup_text(scope: str, owner_id: int, gcfg: dict) -> str:
    received = gcfg.get("last_webhook_received_at")
    received_text = "Received ✅" if received else "Not received yet ⚪"
    return (
        "🔗 Razorpay Webhook Setup\n\n"
        "Your unique webhook URL has been generated automatically.\n\n"
        f"Webhook URL:\n{_webhook_url(scope, owner_id)}\n\n"
        "Required Events:\n"
        "• payment.captured\n"
        "• order.paid\n"
        "• payment_link.paid\n\n"
        "Current Status:\n"
        f"Webhook URL: {'Generated ✅' if PUBLIC_BASE_URL else 'Unavailable ❌'}\n"
        f"Webhook Secret: {'Saved ✅' if gcfg.get('webhook_secret') else 'Not saved ❌'}\n"
        f"Last valid webhook: {received_text}"
    )


def _webhook_keyboard(scope: str):
    return _kb([
        [InlineKeyboardButton("📋 Copy Webhook URL", callback_data=f"pgcfg_{scope}_razorpay_copywebhook")],
        [InlineKeyboardButton("🔐 Set Webhook Secret", callback_data=f"pgcfg_{scope}_razorpay_field_webhook_secret")],
        [InlineKeyboardButton("🧪 Test Webhook", callback_data=f"pgcfg_{scope}_razorpay_testwebhook")],
        [InlineKeyboardButton("📖 Setup Guide", callback_data=f"pgcfg_{scope}_razorpay_guide")],
        [InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_razorpay")],
    ])


def _guide_text() -> str:
    return (
        "📖 Razorpay Webhook Setup Guide\n\n"
        "1. Log in to your Razorpay Dashboard.\n"
        "2. Open Settings → Webhooks.\n"
        "3. Tap Add New Webhook.\n"
        "4. Copy the Webhook URL from this bot and paste it in Razorpay.\n"
        "5. Create a strong Webhook Secret.\n"
        "6. Select payment.captured, order.paid and payment_link.paid.\n"
        "7. Save the webhook.\n"
        "8. Return to this bot and tap Set Webhook Secret.\n"
        "9. Paste the same secret created in Razorpay.\n"
        "10. Tap Test Webhook, then Test Connection.\n\n"
        "Important: Razorpay Key Secret and Webhook Secret are different."
    )


async def gateway_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    if len(parts) < 3:
        return
    scope = parts[1]
    if scope not in {"owner", "seller"}:
        return
    if scope == "owner" and not await is_admin(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    owner_id = _scope_owner(update, scope)
    action = "_".join(parts[2:])
    cfg = await get_gateway_config(scope, owner_id, decrypt=True)

    if action in {"home", "default"}:
        await q.edit_message_text(_payment_header(scope, cfg), reply_markup=_home_keyboard(scope, cfg))
        return

    if action == "history":
        items = await gateway_history(scope, owner_id, 25)
        lines = ["📜 Gateway Payment History", ""]
        for tx in items:
            lines.append(f"• {tx.get('transaction_id')}\n  {tx.get('gateway','-').title()} | ₹{tx.get('amount',0):g} | {tx.get('status','-')}")
        await q.edit_message_text("\n".join(lines) if items else "📜 No gateway payments yet.", reply_markup=_home_keyboard(scope, cfg))
        return

    gateway = action.split("_", 1)[0]
    if gateway not in SUPPORTED_GATEWAYS:
        return
    gcfg = (cfg.get("gateways") or {}).get(gateway) or {}
    suffix = action[len(gateway):].lstrip("_")

    if not suffix:
        await q.edit_message_text(
            _gateway_header(scope, gateway, gcfg, owner_id),
            reply_markup=_gateway_keyboard(scope, gateway, bool(gcfg.get("enabled"))),
        )
        return

    if suffix == "toggle":
        enable = not bool(gcfg.get("enabled"))
        if enable and not gateway_is_ready(gateway, gcfg):
            missing = ", ".join(gateway_missing_fields(gateway, gcfg))
            await q.answer(f"Set credentials first: {missing}", show_alert=True)
            return
        cfg = await save_gateway_config(scope, owner_id, gateway, {"enabled": enable, "mode": "live"})
        gcfg = (cfg.get("gateways") or {}).get(gateway) or {}
        await q.edit_message_text(_gateway_header(scope, gateway, gcfg, owner_id), reply_markup=_gateway_keyboard(scope, gateway, enable))
        return

    if gateway == "razorpay" and suffix == "webhook":
        await q.edit_message_text(_webhook_setup_text(scope, owner_id, gcfg), reply_markup=_webhook_keyboard(scope))
        return

    if gateway == "razorpay" and suffix == "copywebhook":
        await q.edit_message_text(
            "📋 Your Razorpay Webhook URL\n\n"
            "Long-press the URL below and copy it, then paste it into Razorpay Dashboard:\n\n"
            f"{_webhook_url(scope, owner_id)}",
            reply_markup=_kb([
                [InlineKeyboardButton("🔐 Set Webhook Secret", callback_data=f"pgcfg_{scope}_razorpay_field_webhook_secret")],
                [InlineKeyboardButton("📖 Setup Guide", callback_data=f"pgcfg_{scope}_razorpay_guide")],
                [InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_razorpay_webhook")],
            ]),
        )
        return

    if gateway == "razorpay" and suffix == "guide":
        links = await get_official_links()
        rows = [
            [InlineKeyboardButton("📋 Copy Webhook URL", callback_data=f"pgcfg_{scope}_razorpay_copywebhook")],
            [InlineKeyboardButton("🔐 Set Webhook Secret", callback_data=f"pgcfg_{scope}_razorpay_field_webhook_secret")],
        ]
        support_url = links.get("support")
        if support_url:
            rows.append([InlineKeyboardButton("💬 Contact Support", url=support_url)])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_razorpay_webhook")])
        await q.edit_message_text(_guide_text(), reply_markup=_kb(rows))
        return

    if gateway == "razorpay" and suffix == "testwebhook":
        last_received = gcfg.get("last_webhook_received_at")
        if last_received:
            when = last_received.strftime("%Y-%m-%d %H:%M UTC") if isinstance(last_received, datetime) else str(last_received)
            text = (
                "✅ Test Webhook Received\n\n"
                "A valid Razorpay webhook signature was received successfully.\n"
                f"Last received: {when}"
            )
        else:
            text = (
                "🧪 Razorpay Webhook Test\n\n"
                "No valid webhook has been received yet.\n\n"
                "Open Razorpay Dashboard, use Send Test Webhook (or complete a test payment), then tap Check Again."
            )
        await q.edit_message_text(
            text,
            reply_markup=_kb([
                [InlineKeyboardButton("🔄 Check Again", callback_data=f"pgcfg_{scope}_razorpay_testwebhook")],
                [InlineKeyboardButton("📖 Setup Guide", callback_data=f"pgcfg_{scope}_razorpay_guide")],
                [InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_razorpay_webhook")],
            ]),
        )
        return

    if suffix == "test":
        try:
            await test_gateway_connection(scope, owner_id, gateway)
            extra = ""
            if gateway == "razorpay":
                extra = (
                    f"\nWebhook URL: {'Generated ✅' if PUBLIC_BASE_URL else 'Unavailable ❌'}"
                    f"\nWebhook Secret: {'Saved ✅' if gcfg.get('webhook_secret') else 'Missing ❌'}"
                    f"\nValid Webhook Received: {'Yes ✅' if gcfg.get('last_webhook_received_at') else 'Not yet ⚪'}"
                )
            await q.edit_message_text(
                f"✅ {gateway.title()} Connection Successful\n\n"
                "Mode: LIVE\n"
                "Account/API access verified."
                f"{extra}",
                reply_markup=_gateway_keyboard(scope, gateway, bool(gcfg.get("enabled"))),
            )
        except GatewayError as exc:
            await q.edit_message_text(
                f"❌ {gateway.title()} Connection Failed\n\n{exc}",
                reply_markup=_gateway_keyboard(scope, gateway, bool(gcfg.get("enabled"))),
            )
        return

    if suffix.startswith("field_"):
        field = suffix.replace("field_", "", 1)
        allowed = {
            "razorpay": {"key_id", "key_secret", "webhook_secret"},
            "cashfree": {"client_id", "client_secret"},
        }
        if field not in allowed.get(gateway, set()):
            return
        labels = {
            "key_id": "Razorpay Key ID",
            "key_secret": "Razorpay Key Secret",
            "webhook_secret": "the same Razorpay Webhook Secret created in your Razorpay Dashboard",
            "client_id": "Cashfree App ID / Client ID",
            "client_secret": "Cashfree Secret Key",
        }
        context.user_data["pgcfg_wait"] = {"scope": scope, "owner_id": owner_id, "gateway": gateway, "field": field}
        back = f"pgcfg_{scope}_razorpay_webhook" if field == "webhook_secret" else f"pgcfg_{scope}_{gateway}"
        await q.edit_message_text(
            f"Send {labels[field]} in one message.\n\nFor security, your message will be deleted after saving.",
            reply_markup=_kb([[InlineKeyboardButton("⬅ Back", callback_data=back)]]),
        )
        return

    if suffix == "credentials":
        context.user_data["pgcfg_wait"] = {"scope": scope, "owner_id": owner_id, "gateway": gateway}
        help_text = "Send in one message:\nAPP_ID | SECRET_KEY"
        await q.edit_message_text(help_text, reply_markup=_kb([[InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_{gateway}")]]))
        return


async def gateway_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("pgcfg_wait")
    if not state:
        return
    if state["scope"] == "owner" and not await is_admin(update.effective_user.id):
        return
    raw = (update.effective_message.text or "").strip()
    values = [x.strip() for x in raw.split("|")]
    gateway = state["gateway"]
    try:
        if state.get("field"):
            if not raw:
                raise ValueError("Value cannot be empty")
            payload = {state["field"]: raw}
        elif gateway == "cashfree" and len(values) == 2:
            payload = {"client_id": values[0], "client_secret": values[1]}
        else:
            raise ValueError("Invalid format")
        cfg = await save_gateway_config(state["scope"], state["owner_id"], gateway, {**payload, "mode": "live"})
        context.user_data.pop("pgcfg_wait", None)
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        gcfg = (cfg.get("gateways") or {}).get(gateway) or {}
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_gateway_header(state["scope"], gateway, gcfg, state["owner_id"]),
            reply_markup=_gateway_keyboard(state["scope"], gateway, bool(gcfg.get("enabled"))),
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Could not save: {exc}")


async def seller_plan_gateway_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, gateway, request_type, plan_id = q.data.split("_", 3)
    except ValueError:
        return
    plan = await get_paid_plan(plan_id)
    if not plan:
        await q.answer("Plan unavailable", show_alert=True)
        return
    tx = await create_gateway_transaction(
        scope="owner",
        owner_id=0,
        payer_user_id=q.from_user.id,
        gateway=gateway,
        amount=float(plan.get("price", 0)),
        currency="INR",
        purpose="seller_plan",
        reference_id=plan_id,
        metadata={"plan_id": plan_id, "request_type": request_type, "description": f"Seller {plan.get('name')} plan"},
    )
    try:
        checkout = await create_checkout(tx)
    except GatewayError as exc:
        await q.edit_message_text(f"❌ Gateway error: {exc}", reply_markup=_kb([[InlineKeyboardButton("⬅ Back", callback_data="seller_upgrade_plan")]]))
        return
    url = checkout.get("checkout_url")
    await q.edit_message_text(
        f"💳 {gateway.title()} Payment\n\nPlan: {plan.get('name')}\nAmount: ₹{plan.get('price',0):g}\nTransaction: {tx['transaction_id']}\n\nPayment successful hone ke baad plan automatically activate hoga.",
        reply_markup=_kb([[InlineKeyboardButton("💳 Pay Now", url=url)], [InlineKeyboardButton("⬅ Back", callback_data="seller_upgrade_plan")]]),
    )


def handlers():
    return [
        CallbackQueryHandler(gateway_callback, pattern=r"^pgcfg_(owner|seller)_.*$"),
        CallbackQueryHandler(seller_plan_gateway_callback, pattern=r"^pgsp_.*$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, gateway_text),
    ]
