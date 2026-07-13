from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from database.admins import is_admin
from database.payment_gateways import (
    SUPPORTED_GATEWAYS,
    create_gateway_transaction,
    gateway_history,
    get_gateway_config,
    save_gateway_config,
    set_gateway_preferences,
)
from database.seller_subscriptions import get_paid_plan
from services.payment_gateways import GatewayError, create_checkout


def _kb(rows):
    return InlineKeyboardMarkup(rows)


def _scope_owner(update: Update, scope: str) -> int:
    return 0 if scope == "owner" else int(update.effective_user.id)


def _home_keyboard(scope: str):
    rows = []
    for name, title in (("razorpay", "Razorpay"), ("cashfree", "Cashfree"), ("phonepe", "PhonePe PG"), ("paytm", "Paytm PG")):
        rows.append([InlineKeyboardButton(f"💳 {title}", callback_data=f"pgcfg_{scope}_{name}")])
    rows += [
        [InlineKeyboardButton("⚙ Default Gateway", callback_data=f"pgcfg_{scope}_default")],
        [InlineKeyboardButton("📜 Gateway History", callback_data=f"pgcfg_{scope}_history")],
        [InlineKeyboardButton("⬅ Back", callback_data="sub_mgmt_home" if scope == "owner" else "main_seller_dashboard")],
    ]
    return _kb(rows)


def _gateway_keyboard(scope: str, gateway: str, enabled: bool):
    return _kb([
        [InlineKeyboardButton("⛔ Disable" if enabled else "✅ Enable", callback_data=f"pgcfg_{scope}_{gateway}_toggle")],
        [InlineKeyboardButton("🔑 Set Credentials", callback_data=f"pgcfg_{scope}_{gateway}_credentials")],
        [InlineKeyboardButton("🧪 Test Mode", callback_data=f"pgcfg_{scope}_{gateway}_mode_test"), InlineKeyboardButton("🚀 Live Mode", callback_data=f"pgcfg_{scope}_{gateway}_mode_live")],
        [InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_home")],
    ])


def _credential_help(gateway: str) -> str:
    return {
        "razorpay": "Send:\nKEY_ID | KEY_SECRET | WEBHOOK_SECRET",
        "cashfree": "Send:\nCLIENT_ID | CLIENT_SECRET",
        "phonepe": "Send:\nCLIENT_ID | CLIENT_VERSION | CLIENT_SECRET | WEBHOOK_USERNAME | WEBHOOK_PASSWORD",
        "paytm": "Send:\nMID | MERCHANT_KEY | WEBSITE_NAME",
    }[gateway]


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

    if action == "home":
        enabled = [g for g in SUPPORTED_GATEWAYS if (cfg.get("gateways") or {}).get(g, {}).get("enabled")]
        await q.edit_message_text(
            f"💳 {'Owner' if scope == 'owner' else 'Seller'} Payment Gateways\n\n"
            f"Default: {cfg.get('default_gateway', 'manual').title()}\n"
            f"Manual payment: {'Enabled' if cfg.get('manual_enabled', True) else 'Disabled'}\n"
            f"Enabled: {', '.join(x.title() for x in enabled) or 'None'}",
            reply_markup=_home_keyboard(scope),
        )
        return

    if action == "default":
        rows = [[InlineKeyboardButton("Manual Screenshot", callback_data=f"pgcfg_{scope}_setdefault_manual")]]
        for gateway in SUPPORTED_GATEWAYS:
            rows.append([InlineKeyboardButton(gateway.title(), callback_data=f"pgcfg_{scope}_setdefault_{gateway}")])
        rows.append([InlineKeyboardButton("🔄 Manual On/Off", callback_data=f"pgcfg_{scope}_manualtoggle")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_home")])
        await q.edit_message_text("⚙ Choose default payment method", reply_markup=_kb(rows))
        return

    if action == "manualtoggle":
        await set_gateway_preferences(scope, owner_id, manual_enabled=not cfg.get("manual_enabled", True))
        await q.edit_message_text("✅ Manual payment setting updated.", reply_markup=_home_keyboard(scope))
        return

    if action.startswith("setdefault_"):
        gateway = action.replace("setdefault_", "")
        if gateway != "manual" and not (cfg.get("gateways") or {}).get(gateway, {}).get("enabled"):
            await q.answer("Enable this gateway first", show_alert=True)
            return
        await set_gateway_preferences(scope, owner_id, default_gateway=gateway)
        await q.edit_message_text(f"✅ Default gateway: {gateway.title()}", reply_markup=_home_keyboard(scope))
        return

    if action == "history":
        items = await gateway_history(scope, owner_id, 25)
        lines = ["📜 Gateway Payment History", ""]
        for tx in items:
            lines.append(f"• {tx.get('transaction_id')}\n  {tx.get('gateway','-').title()} | ₹{tx.get('amount',0):g} | {tx.get('status','-')}")
        await q.edit_message_text("\n".join(lines) if items else "📜 No gateway payments yet.", reply_markup=_home_keyboard(scope))
        return

    gateway = action.split("_", 1)[0]
    if gateway not in SUPPORTED_GATEWAYS:
        return
    gcfg = (cfg.get("gateways") or {}).get(gateway) or {}
    suffix = action[len(gateway):].lstrip("_")

    if not suffix:
        masked = []
        for key, value in gcfg.items():
            if key in {"key_secret", "webhook_secret", "client_secret", "webhook_password", "merchant_key"}:
                masked.append(f"{key}: {'Set' if value else 'Not set'}")
            elif key not in {"enabled"}:
                masked.append(f"{key}: {value}")
        await q.edit_message_text(
            f"💳 {gateway.title()}\n\nStatus: {'Enabled' if gcfg.get('enabled') else 'Disabled'}\nMode: {gcfg.get('mode','test').title()}\n" + ("\n".join(masked) if masked else "Credentials not configured"),
            reply_markup=_gateway_keyboard(scope, gateway, bool(gcfg.get("enabled"))),
        )
        return

    if suffix == "toggle":
        await save_gateway_config(scope, owner_id, gateway, {"enabled": not bool(gcfg.get("enabled"))})
        await q.edit_message_text("✅ Gateway status updated.", reply_markup=_home_keyboard(scope))
        return

    if suffix.startswith("mode_"):
        mode = suffix.replace("mode_", "")
        await save_gateway_config(scope, owner_id, gateway, {"mode": mode})
        await q.edit_message_text(f"✅ {gateway.title()} mode set to {mode.title()}.", reply_markup=_home_keyboard(scope))
        return

    if suffix == "credentials":
        context.user_data["pgcfg_wait"] = {"scope": scope, "owner_id": owner_id, "gateway": gateway}
        await q.edit_message_text(_credential_help(gateway), reply_markup=_kb([[InlineKeyboardButton("⬅ Back", callback_data=f"pgcfg_{scope}_{gateway}")]]))
        return


async def gateway_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("pgcfg_wait")
    if not state:
        return
    if state["scope"] == "owner" and not await is_admin(update.effective_user.id):
        return
    values = [x.strip() for x in (update.effective_message.text or "").split("|")]
    gateway = state["gateway"]
    try:
        if gateway == "razorpay" and len(values) == 3:
            payload = {"key_id": values[0], "key_secret": values[1], "webhook_secret": values[2]}
        elif gateway == "cashfree" and len(values) == 2:
            payload = {"client_id": values[0], "client_secret": values[1]}
        elif gateway == "phonepe" and len(values) == 5:
            payload = {"client_id": values[0], "client_version": values[1], "client_secret": values[2], "webhook_username": values[3], "webhook_password": values[4]}
        elif gateway == "paytm" and len(values) == 3:
            payload = {"mid": values[0], "merchant_key": values[1], "website_name": values[2]}
        else:
            raise ValueError("Invalid format")
        await save_gateway_config(state["scope"], state["owner_id"], gateway, payload)
        context.user_data.pop("pgcfg_wait", None)
        await update.effective_message.reply_text("✅ Gateway credentials saved securely.", reply_markup=_home_keyboard(state["scope"]))
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Could not save: {exc}\n\n{_credential_help(gateway)}")


async def seller_plan_gateway_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # pgsp_<gateway>_<request_type>_<plan_id>
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
