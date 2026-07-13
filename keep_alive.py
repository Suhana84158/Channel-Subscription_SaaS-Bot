import asyncio
import json
import os
from threading import Thread

from flask import Flask, jsonify, redirect, request

app = Flask(__name__)
_runtime_loop = None
_main_bot = None


def configure_runtime(loop, main_bot):
    global _runtime_loop, _main_bot
    _runtime_loop = loop
    _main_bot = main_bot


def _run(coro, timeout=45):
    if _runtime_loop is None:
        raise RuntimeError("Bot runtime is not ready")
    return asyncio.run_coroutine_threadsafe(coro, _runtime_loop).result(timeout=timeout)


@app.route("/")
def home():
    return {"status": "online", "service": "Telegram Subscription Bot", "version": "2.1-gateways"}


@app.route("/health")
def health():
    return {"status": "healthy", "runtime": bool(_runtime_loop)}


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
    if not tx or not tx.get("payment_session_id"):
        return "Invalid or expired payment session", 404
    mode = "sandbox" if tx.get("gateway_mode", "test") == "test" else "production"
    session_id = json.dumps(tx["payment_session_id"])
    return f"""
<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<script src='https://sdk.cashfree.com/js/v3/cashfree.js'></script></head>
<body style='font-family:sans-serif;text-align:center;padding:30px'>
<h2>Cashfree Secure Checkout</h2><p>Transaction: {transaction_id}</p>
<button id='pay' style='padding:14px 24px;font-size:18px'>Pay Now</button>
<script>
const cashfree = Cashfree({{mode: {json.dumps(mode)}}});
document.getElementById('pay').onclick = () => cashfree.checkout({{paymentSessionId: {session_id}, redirectTarget: '_self'}});
</script></body></html>"""


@app.route("/checkout/paytm/<transaction_id>")
def paytm_checkout(transaction_id):
    from database.payment_gateways import get_gateway_transaction
    tx = _run(get_gateway_transaction(transaction_id))
    if not tx or not tx.get("txn_token"):
        return "Invalid or expired payment session", 404
    host = tx.get("paytm_host", "https://securestage.paytmpayments.com")
    mid = tx.get("paytm_mid", "")
    token = tx["txn_token"]
    amount = f"{float(tx.get('amount', 0)):.2f}"
    action = f"{host}/theia/api/v1/showPaymentPage?mid={mid}&orderId={transaction_id}"
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
            await _main_bot.send_message(
                tx["payer_user_id"],
                f"✅ Payment verified automatically\n\nGateway: {tx.get('gateway','').title()}\nAmount: ₹{tx.get('amount',0):g}\nYour seller plan is now active.",
            )
        return
    if tx.get("purpose") == "child_subscription":
        from database.seller_data import get_channels, get_subscription
        from services.bot_manager import bot_manager
        running = bot_manager.get_running(tx["owner_id"])
        if not running:
            return
        bot = running.application.bot
        links = []
        for channel in await get_channels(tx["owner_id"]):
            try:
                invite = await bot.create_chat_invite_link(channel["chat_id"], member_limit=1)
                links.append(f"{channel.get('title','Premium Channel')}\n{invite.invite_link}")
            except Exception:
                continue
        sub = await get_subscription(tx["owner_id"], tx["payer_user_id"])
        text = (
            f"✅ Payment verified automatically\n\nGateway: {tx.get('gateway','').title()}\n"
            f"Amount: ₹{tx.get('amount',0):g}\nPlan: {tx.get('metadata',{}).get('plan_name','Subscription')}\n"
        )
        if sub and sub.get("expiry_date"):
            text += f"Expiry: {sub['expiry_date'].strftime('%d-%m-%Y %H:%M UTC')}\n"
        if links:
            text += "\nJoin using your private invite link(s):\n\n" + "\n\n".join(links)
        await bot.send_message(tx["payer_user_id"], text, disable_web_page_preview=True)


@app.route("/webhooks/<gateway>/<scope>/<int:owner_id>", methods=["POST"])
def gateway_webhook(gateway, scope, owner_id):
    from services.payment_gateways import verify_and_process_webhook
    raw = request.get_data(cache=True)
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form.to_dict(flat=True)
    try:
        ok, message = _run(verify_and_process_webhook(gateway, scope, owner_id, {k.lower(): v for k, v in request.headers.items()}, raw, payload))
        txid = payload.get("ORDERID") or payload.get("orderId")
        if not txid and gateway == "razorpay":
            rp = payload.get("payload") or {}
            payment = ((rp.get("payment") or {}).get("entity") or {})
            payment_link = ((rp.get("payment_link") or {}).get("entity") or {})
            txid = (payment.get("notes") or {}).get("transaction_id") or payment_link.get("reference_id")
        if not txid and isinstance(payload.get("data"), dict):
            txid = ((payload.get("data") or {}).get("order") or {}).get("order_id")
        if not txid and isinstance(payload.get("payload"), dict):
            body = payload.get("payload") or {}
            txid = body.get("merchantOrderId") or ((body.get("metaInfo") or {}).get("udf1"))
        if txid:
            try:
                _run(_notify_success(str(txid)), timeout=30)
            except Exception:
                pass
        return jsonify({"ok": ok, "message": message}), 200 if ok else 401
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def run():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False, use_reloader=False)


def keep_alive():
    Thread(target=run, daemon=True).start()
