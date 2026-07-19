import asyncio
import html
import json
import os
import platform
import sys
import time
from threading import Thread

from flask import Flask, jsonify, make_response, request

app = Flask(__name__)
_runtime_loop = None
_main_bot = None
_started_monotonic = time.monotonic()
_started_at_unix = time.time()

SERVICE_VERSION = os.getenv("APP_VERSION", "2.2-runtime-stability")


def configure_runtime(loop, main_bot):
    global _runtime_loop, _main_bot
    _runtime_loop = loop
    _main_bot = main_bot


def _run(coro, timeout=45):
    if _runtime_loop is None:
        raise RuntimeError("Bot runtime is not ready")
    return asyncio.run_coroutine_threadsafe(
        coro,
        _runtime_loop,
    ).result(timeout=timeout)


def _memory_mb():
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return round(usage / (1024 * 1024), 2)
        return round(usage / 1024, 2)
    except Exception:
        return None


async def _runtime_health():
    from database.mongo import (
        ensure_database,
        get_database_health,
        ping_database,
    )
    from scheduler import scheduler_health
    from database.health_monitoring import record_health_snapshot
    from services.bot_manager import bot_manager

    mongo_ok = await ping_database(timeout=4, log_failure=False)

    if not mongo_ok:
        try:
            await ensure_database(max_attempts=2, ping_timeout=4)
            mongo_ok = await ping_database(
                timeout=4,
                log_failure=False,
            )
        except Exception:
            mongo_ok = False

    database = get_database_health()
    database["status"] = "connected" if mongo_ok else "disconnected"

    # ZIP 28 exposes ``runtime_health()``. Older patch builds used
    # ``get_runtime_status()``, so support both names without changing the
    # clone-bot manager API or breaking existing installations.
    clone_status_method = (
        getattr(bot_manager, "runtime_health", None)
        or getattr(bot_manager, "get_runtime_status", None)
    )

    if callable(clone_status_method):
        clone_bots = clone_status_method()
        if asyncio.iscoroutine(clone_bots):
            clone_bots = await clone_bots
    else:
        running = getattr(bot_manager, "_running", {})
        clone_bots = {
            "active": len(running),
            "running": len(running),
            "offline": 0,
            "unhealthy": 0,
            "recovery_attempts_total": 0,
        }

    scheduler = scheduler_health()

    raw_healthy = bool(
        mongo_ok
        and scheduler.get("running")
        and _runtime_loop is not None
    )
    monitor = await record_health_snapshot(
        source="http_health",
        raw_healthy=raw_healthy,
        details={
            "database_ok": bool(mongo_ok),
            "scheduler_running": bool(scheduler.get("running")),
            "runtime_ready": _runtime_loop is not None,
            "clone_offline": int(clone_bots.get("offline", 0) or 0),
            "clone_unhealthy": int(clone_bots.get("unhealthy", 0) or 0),
        },
    )

    return {
        "status": monitor["status"],
        "service": "Telegram Subscription SaaS Bot",
        "version": SERVICE_VERSION,
        "runtime_ready": _runtime_loop is not None,
        "uptime_seconds": int(time.monotonic() - _started_monotonic),
        "started_at_unix": int(_started_at_unix),
        "health_monitor": monitor,
        "database": database,
        "scheduler": scheduler,
        "clone_bots": clone_bots,
        "system": {
            "memory_mb": _memory_mb(),
            "python": platform.python_version(),
            "platform": platform.system(),
        },
    }


@app.route("/")
def home():
    return {
        "status": "online",
        "service": "Telegram Subscription SaaS Bot",
        "version": SERVICE_VERSION,
    }


@app.route("/health")
def health():
    if _runtime_loop is None:
        payload = {
            "status": "starting",
            "service": "Telegram Subscription SaaS Bot",
            "version": SERVICE_VERSION,
            "runtime_ready": False,
            "uptime_seconds": int(
                time.monotonic() - _started_monotonic
            ),
        }
        return jsonify(payload), 503

    try:
        payload = _run(_runtime_health(), timeout=15)
        # A transient failure is reported as "degraded" but still returns 200.
        # External monitors should mark the service down only after the configured
        # consecutive-failure threshold changes the state to "unhealthy".
        status_code = 503 if payload.get("status") == "unhealthy" else 200
        return jsonify(payload), status_code
    except Exception as exc:
        return jsonify(
            {
                "status": "unhealthy",
                "service": "Telegram Subscription SaaS Bot",
                "version": SERVICE_VERSION,
                "runtime_ready": True,
                "error": type(exc).__name__,
                "uptime_seconds": int(
                    time.monotonic() - _started_monotonic
                ),
            }
        ), 503


@app.route("/payment/return/<transaction_id>", methods=["GET", "POST"])
def payment_return(transaction_id):
    return (
        "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
        "<h2>Payment status is being verified</h2>"
        f"<p>Transaction: {transaction_id}</p>"
        "<p>You may return to Telegram. Activation happens only after secure gateway verification.</p>"
        "</body></html>"
    )


@app.route("/checkout/cashfree/<transaction_id>")
def cashfree_checkout(transaction_id):
    from database.payment_gateways import get_gateway_transaction

    tx = _run(get_gateway_transaction(transaction_id))
    if (
        not tx
        or tx.get("gateway") != "cashfree"
        or not tx.get("payment_session_id")
        or tx.get("status") in {"failed", "fulfilled"}
    ):
        return "Invalid or expired Cashfree payment session", 404

    mode = (
        "sandbox"
        if tx.get("gateway_mode", "test") == "test"
        else "production"
    )
    session_id = json.dumps(str(tx["payment_session_id"]))
    safe_transaction_id = html.escape(str(transaction_id))

    page = f"""
<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Cashfree Secure Checkout</title>
<script src='https://sdk.cashfree.com/js/v3/cashfree.js'></script>
</head>
<body style='font-family:sans-serif;text-align:center;padding:30px'>
<h2>Cashfree Secure Checkout</h2>
<p>Transaction: {safe_transaction_id}</p>
<button id='pay' style='padding:14px 24px;font-size:18px'>Pay Now</button>
<p id='error' style='color:#b00020'></p>
<script>
const cashfree = Cashfree({{mode: {json.dumps(mode)}}});
const button = document.getElementById('pay');
button.onclick = async () => {{
  button.disabled = true;
  document.getElementById('error').textContent = '';
  try {{
    await cashfree.checkout({{
      paymentSessionId: {session_id},
      redirectTarget: '_self'
    }});
  }} catch (error) {{
    document.getElementById('error').textContent =
      'Unable to open checkout. Please return to Telegram and try again.';
    button.disabled = false;
  }}
}};
</script>
</body>
</html>
"""
    response = make_response(page, 200)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/checkout/paytm/<transaction_id>")
def paytm_checkout(transaction_id):
    from database.payment_gateways import get_gateway_transaction

    tx = _run(get_gateway_transaction(transaction_id))
    if not tx or not tx.get("txn_token"):
        return "Invalid or expired payment session", 404

    host = tx.get(
        "paytm_host",
        "https://securestage.paytmpayments.com",
    )
    mid = tx.get("paytm_mid", "")
    token = tx["txn_token"]
    amount = f"{float(tx.get('amount', 0)):.2f}"
    action = (
        f"{host}/theia/api/v1/showPaymentPage"
        f"?mid={mid}&orderId={transaction_id}"
    )

    return f"""
<!doctype html><html><body onload='document.forms[0].submit()'>
<form method='post' action='{action}'>
<input type='hidden' name='mid' value='{mid}'><input type='hidden' name='orderId' value='{transaction_id}'>
<input type='hidden' name='txnToken' value='{token}'><input type='hidden' name='amount' value='{amount}'>
</form><p>Opening Paytm secure checkout...</p></body></html>"""


async def _notify_success(transaction_id):
    from database.payment_gateways import get_gateway_transaction

    tx = await get_gateway_transaction(transaction_id)
    if not tx or tx.get("status") != "fulfilled":
        return

    if tx.get("purpose") == "seller_plan":
        if _main_bot:
            from config import ADMIN_IDS
            from database.seller_subscriptions import get_assignment, get_paid_plan

            fulfillment = tx.get("fulfillment") or {}
            metadata = tx.get("metadata") or {}
            plan = await get_paid_plan(
                fulfillment.get("plan_id") or metadata.get("plan_id", "")
            ) or {}

            try:
                seller_chat = await _main_bot.get_chat(int(tx["payer_user_id"]))
                seller_name = getattr(seller_chat, "full_name", None) or "Unknown"
                seller_username = getattr(seller_chat, "username", None)
            except Exception:
                seller_name = str(metadata.get("customer_name") or "Unknown")
                seller_username = metadata.get("username")

            def _format_dt(value):
                if not value:
                    return "-"
                if hasattr(value, "strftime"):
                    return value.strftime("%d %b %Y, %I:%M %p UTC")
                return str(value)

            username_text = f"@{seller_username}" if seller_username else "Not Set"
            payment_date = tx.get("paid_at") or tx.get("fulfilled_at") or tx.get("updated_at")
            expiry_date = fulfillment.get("expiry_date")
            if not expiry_date:
                assignment = await get_assignment(int(tx["payer_user_id"]))
                expiry_date = (assignment or {}).get("expiry_date")
            duration_days = int(plan.get("duration_days") or 0)
            duration_text = f"{duration_days} days" if duration_days else "Unlimited"
            plan_name = str(plan.get("name") or metadata.get("description") or "Seller Plan")

            limits_text = (
                f"• Clone Bots: {int(plan.get('bot_limit', 0))}\n"
                f"• Active Subscribers: {int(plan.get('active_subscriber_limit', 0))}\n"
                f"• Channels/Groups: {int(plan.get('channel_limit', 0))}\n"
                f"• Subscription Plans: {int(plan.get('plan_limit', 0))}\n"
                f"• Admins: {int(plan.get('admin_limit', 0))}"
            )

            details = (
                "✅ Payment verified automatically\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Seller Name: {seller_name}\n"
                f"🆔 Seller Username: {username_text}\n"
                f"🔢 Seller ID: {tx.get('payer_user_id')}\n"
                f"📦 Plan Name: {plan_name}\n"
                f"💰 Amount: ₹{float(tx.get('amount') or 0):g}\n"
                f"💳 Gateway: {str(tx.get('gateway') or '').title() or '-'}\n"
                f"🧾 Transaction ID: {tx.get('transaction_id') or '-'}\n"
                f"📅 Payment Date: {_format_dt(payment_date)}\n"
                f"⏳ Expiry Date: {_format_dt(expiry_date)}\n"
                f"⌛ Duration: {duration_text}\n"
                f"🧾 Invoice: {fulfillment.get('invoice_no', '-')}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 Plan Limitations\n"
                f"{limits_text}"
            )

            await _main_bot.send_message(
                int(tx["payer_user_id"]),
                details + "\n\n✅ Your seller plan is now active.",
            )

            owner_notice = (
                "💰 Seller plan activated automatically\n\n"
                + details.replace("✅ Payment verified automatically", "✅ Payment verified")
            )
            for admin_id in {int(value) for value in ADMIN_IDS}:
                try:
                    await _main_bot.send_message(admin_id, owner_notice)
                except Exception:
                    logger.exception(
                        "Failed to notify owner about seller plan payment "
                        "admin_id=%s seller_id=%s transaction_id=%s",
                        admin_id,
                        tx.get("payer_user_id"),
                        tx.get("transaction_id"),
                    )
        return

    if tx.get("purpose") == "child_subscription":
        # services.payment_gateways.fulfill_transaction() already activates the
        # subscription and calls deliver_subscription_access(), which sends the
        # single success message and fresh invite link(s). Sending from this
        # return/webhook notifier as well creates duplicate links.
        return


@app.route(
    "/webhooks/<gateway>/<scope>/<int:owner_id>",
    methods=["POST"],
)
def gateway_webhook(gateway, scope, owner_id):
    from services.payment_gateways import verify_and_process_webhook

    if gateway not in {"razorpay", "cashfree", "phonepe", "paytm"}:
        return jsonify({"ok": False, "error": "Unsupported gateway"}), 404
    if scope not in {"owner", "seller"}:
        return jsonify({"ok": False, "error": "Invalid payment scope"}), 404

    raw = request.get_data(cache=True)
    if len(raw) > 1_000_000:
        return jsonify({"ok": False, "error": "Webhook body too large"}), 413

    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form.to_dict(flat=True)

    try:
        ok, message = _run(
            verify_and_process_webhook(
                gateway,
                scope,
                owner_id,
                {
                    key.lower(): value
                    for key, value in request.headers.items()
                },
                raw,
                payload,
            )
        )

        txid = payload.get("ORDERID") or payload.get("orderId")

        if not txid and gateway == "razorpay":
            razorpay_payload = payload.get("payload") or {}
            payment = (
                (razorpay_payload.get("payment") or {})
                .get("entity")
                or {}
            )
            payment_link = (
                (razorpay_payload.get("payment_link") or {})
                .get("entity")
                or {}
            )
            txid = (
                (payment.get("notes") or {})
                .get("transaction_id")
                or payment_link.get("reference_id")
            )

        if not txid and isinstance(payload.get("data"), dict):
            txid = (
                ((payload.get("data") or {}).get("order") or {})
                .get("order_id")
            )

        if not txid and isinstance(payload.get("payload"), dict):
            body = payload.get("payload") or {}
            txid = (
                body.get("merchantOrderId")
                or (body.get("metaInfo") or {}).get("udf1")
            )

        # Notify only when this webhook performed the first successful
        # fulfillment. Duplicate webhook deliveries return "already processed"
        # or "already processing" and must not send another success message.
        if txid and ok and message == "processed":
            try:
                _run(_notify_success(str(txid)), timeout=30)
            except Exception:
                pass

        return (
            jsonify({"ok": ok, "message": message}),
            200 if ok else 401,
        )
    except Exception as exc:
        return jsonify(
            {"ok": False, "error": str(exc)}
        ), 500


def run():
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        debug=False,
        use_reloader=False,
    )


def keep_alive():
    Thread(
        target=run,
        daemon=True,
        name="health-server",
    ).start()
