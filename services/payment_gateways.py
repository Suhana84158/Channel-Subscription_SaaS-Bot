from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import aiohttp
from paytmchecksum import PaytmChecksum

from config import PUBLIC_BASE_URL
from database.payment_gateways import (
    claim_transaction_fulfillment,
    claim_transaction_success,
    claim_transaction_notification,
    complete_transaction_notification,
    fail_transaction_notification,
    get_gateway_config,
    get_gateway_transaction,
    get_transaction_by_gateway_order,
    mark_transaction_failed,
    mark_transaction_fulfilled,
    mark_transaction_fulfillment_retry,
    reserve_webhook_event,
    mark_valid_webhook_received,
    update_gateway_transaction,
)
from database.seller_data import activate_subscription, get_plan, create_automatic_payment, get_subscription
from database.seller_subscriptions import get_paid_plan, process_verified_plan_purchase
from database.platform_features import create_invoice, audit


class GatewayError(RuntimeError):
    pass


def _base_url() -> str:
    value = (PUBLIC_BASE_URL or "").rstrip("/")
    if not value:
        raise GatewayError("PUBLIC_BASE_URL is not configured")
    return value


async def _request(method: str, url: str, **kwargs) -> dict:
    status, data = await _request_with_status(method, url, **kwargs)
    if status >= 400:
        raise GatewayError(f"Gateway HTTP {status}: {data}")
    return data


async def _request_with_status(method: str, url: str, **kwargs) -> tuple[int, dict]:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, **kwargs) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"raw": text}
            return response.status, data


def _cashfree_base(mode: str) -> str:
    return "https://sandbox.cashfree.com/pg" if mode == "test" else "https://api.cashfree.com/pg"


def _cashfree_headers(settings: dict, *, idempotency_key: str | None = None) -> dict[str, str]:
    client_id = settings.get("client_id")
    client_secret = settings.get("client_secret")
    if not client_id or not client_secret:
        raise GatewayError("Cashfree App ID or Secret Key is missing")
    headers = {
        "x-client-id": str(client_id),
        "x-client-secret": str(client_secret),
        "x-api-version": "2025-01-01",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["x-idempotency-key"] = idempotency_key
    return headers


def _amount_matches(left: Any, right: Any, tolerance: float = 0.01) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


async def _verify_cashfree_payment(
    transaction: dict,
    settings: dict,
) -> tuple[str, dict]:
    """Re-query Cashfree before fulfillment and verify the paid order details."""
    order_id = str(transaction["transaction_id"])
    mode = str(transaction.get("gateway_mode") or settings.get("mode") or "live").lower()
    if mode not in {"test", "live"}:
        mode = str(s.get("mode") or "live").lower()
    if mode not in {"test", "live"}:
        mode = "live"
    base = _cashfree_base(mode)
    headers = _cashfree_headers(settings)

    order = await _request(
        "GET",
        f"{base}/orders/{order_id}",
        headers=headers,
    )
    if str(order.get("order_status", "")).upper() != "PAID":
        raise GatewayError("Cashfree order is not PAID")
    if str(order.get("order_currency", "")).upper() != str(
        transaction.get("currency", "INR")
    ).upper():
        raise GatewayError("Cashfree currency mismatch")
    if not _amount_matches(order.get("order_amount"), transaction.get("amount")):
        raise GatewayError("Cashfree amount mismatch")

    payments = await _request(
        "GET",
        f"{base}/orders/{order_id}/payments",
        headers=headers,
    )
    if not isinstance(payments, list):
        raise GatewayError("Cashfree payment verification response is invalid")

    successful = next(
        (
            item
            for item in reversed(payments)
            if str(item.get("payment_status", "")).upper() == "SUCCESS"
            and str(item.get("payment_currency", transaction.get("currency", "INR"))).upper()
            == str(transaction.get("currency", "INR")).upper()
            and _amount_matches(item.get("payment_amount"), transaction.get("amount"))
        ),
        None,
    )
    if not successful:
        raise GatewayError("No matching successful Cashfree payment found")

    payment_id = str(successful.get("cf_payment_id") or "")
    if not payment_id:
        raise GatewayError("Cashfree payment ID is missing")
    return payment_id, {"order": order, "payment": successful}


async def test_gateway_connection(scope: str, owner_id: int, gateway: str) -> dict:
    """Validate stored credentials without creating or charging a payment."""
    cfg = await get_gateway_config(scope, owner_id, decrypt=True)
    settings = (cfg.get("gateways") or {}).get(gateway) or {}
    mode = "live"
    if gateway == "razorpay":
        key_id, key_secret = settings.get("key_id"), settings.get("key_secret")
        if not key_id or not key_secret:
            raise GatewayError("Razorpay Key ID or Key Secret is missing")
        auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        await _request(
            "GET",
            "https://api.razorpay.com/v1/orders?count=1",
            headers={"Authorization": f"Basic {auth}"},
        )
        return {"ok": True, "gateway": gateway, "mode": mode}
    if gateway == "cashfree":
        # A missing test order should return 404 when authentication is valid.
        # Authentication failures return 401/403, so no real payment/order is created.
        probe_order = f"connection_test_{int(time.time())}"
        status, data = await _request_with_status(
            "GET",
            f"{_cashfree_base(mode)}/orders/{probe_order}",
            headers=_cashfree_headers(settings),
        )
        if status in {200, 404}:
            return {"ok": True, "gateway": gateway, "mode": mode}
        raise GatewayError(f"Cashfree authentication failed (HTTP {status}): {data}")
    raise GatewayError(f"Connection test for {gateway.title()} is not enabled in this launch version")


async def create_checkout(transaction: dict) -> dict:
    cfg = await get_gateway_config(transaction["scope"], transaction["owner_id"], decrypt=True)
    gateway = transaction["gateway"]
    settings = (cfg.get("gateways") or {}).get(gateway) or {}
    if not settings.get("enabled"):
        raise GatewayError(f"{gateway.title()} is disabled")
    if gateway == "razorpay":
        result = await _create_razorpay(transaction, settings)
    elif gateway == "cashfree":
        result = await _create_cashfree(transaction, settings)
    elif gateway == "phonepe":
        result = await _create_phonepe(transaction, settings)
    elif gateway == "paytm":
        result = await _create_paytm(transaction, settings)
    else:
        raise GatewayError("Unsupported gateway")
    await update_gateway_transaction(transaction["transaction_id"], **result)
    return result


async def _create_razorpay(tx: dict, s: dict) -> dict:
    key_id, key_secret = s.get("key_id"), s.get("key_secret")
    if not key_id or not key_secret:
        raise GatewayError("Razorpay credentials are incomplete")
    auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    payload = {
        "amount": int(round(tx["amount"] * 100)),
        "currency": tx["currency"],
        "reference_id": tx["transaction_id"],
        "description": tx["metadata"].get("description", tx["purpose"]),
        "callback_url": f"{_base_url()}/payment/return/{tx['transaction_id']}",
        "callback_method": "get",
        "notes": {"transaction_id": tx["transaction_id"], "scope": tx["scope"], "owner_id": str(tx["owner_id"])},
    }
    data = await _request("POST", "https://api.razorpay.com/v1/payment_links", headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"}, json=payload)
    return {"gateway_order_id": data.get("id", ""), "checkout_url": data.get("short_url", ""), "gateway_response": data, "status": "pending"}


async def _create_cashfree(tx: dict, s: dict) -> dict:
    mode = "live"
    base = _cashfree_base(mode)
    amount = round(float(tx["amount"]), 2)
    if amount <= 0:
        raise GatewayError("Cashfree amount must be greater than zero")

    metadata = tx.get("metadata") or {}
    raw_phone = "".join(ch for ch in str(metadata.get("phone", "")) if ch.isdigit())
    phone = raw_phone[-10:] if len(raw_phone) >= 10 else "9999999999"
    email = str(
        metadata.get("email")
        or f"telegram{tx['payer_user_id']}@example.com"
    ).strip()

    payload = {
        "order_id": tx["transaction_id"],
        "order_amount": amount,
        "order_currency": str(tx["currency"]).upper(),
        "customer_details": {
            "customer_id": str(tx["payer_user_id"]),
            "customer_phone": phone,
            "customer_email": email,
        },
        "order_meta": {
            "return_url": f"{_base_url()}/payment/return/{tx['transaction_id']}",
            "notify_url": f"{_base_url()}/webhooks/cashfree/{tx['scope']}/{tx['owner_id']}",
        },
        "order_note": metadata.get("description", tx["purpose"]),
    }
    data = await _request(
        "POST",
        f"{base}/orders",
        headers=_cashfree_headers(s, idempotency_key=tx["transaction_id"]),
        json=payload,
    )
    session_id = str(data.get("payment_session_id") or "")
    if not session_id:
        raise GatewayError("Cashfree payment session was not returned")

    checkout = f"{_base_url()}/checkout/cashfree/{tx['transaction_id']}"
    return {
        "gateway_order_id": tx["transaction_id"],
        "cashfree_cf_order_id": str(data.get("cf_order_id") or ""),
        "payment_session_id": session_id,
        "checkout_url": checkout,
        "gateway_response": data,
        "gateway_mode": mode,
        "status": "pending",
    }


async def _phonepe_token(s: dict) -> str:
    if not s.get("client_id") or not s.get("client_secret"):
        raise GatewayError("PhonePe credentials are incomplete")
    base = "https://api-preprod.phonepe.com/apis/pg-sandbox" if s.get("mode", "test") == "test" else "https://api.phonepe.com/apis/identity-manager"
    data = await _request("POST", f"{base}/v1/oauth/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, data={"client_id": s["client_id"], "client_version": str(s.get("client_version", "1")), "client_secret": s["client_secret"], "grant_type": "client_credentials"})
    token = data.get("access_token")
    if not token:
        raise GatewayError(f"PhonePe token missing: {data}")
    return token


async def _create_phonepe(tx: dict, s: dict) -> dict:
    token = await _phonepe_token(s)
    base = "https://api-preprod.phonepe.com/apis/pg-sandbox" if s.get("mode", "test") == "test" else "https://api.phonepe.com/apis/pg"
    payload = {
        "merchantOrderId": tx["transaction_id"],
        "amount": int(round(tx["amount"] * 100)),
        "expireAfter": 1200,
        "paymentFlow": {"type": "PG_CHECKOUT", "merchantUrls": {"redirectUrl": f"{_base_url()}/payment/return/{tx['transaction_id']}"}},
        "disablePaymentRetry": False,
        "metaInfo": {"udf1": tx["transaction_id"], "udf2": tx["scope"], "udf3": str(tx["owner_id"]), "udf4": tx["purpose"]},
    }
    data = await _request("POST", f"{base}/checkout/v2/pay", headers={"Authorization": f"O-Bearer {token}", "Content-Type": "application/json"}, json=payload)
    return {"gateway_order_id": data.get("orderId", tx["transaction_id"]), "checkout_url": data.get("redirectUrl", ""), "gateway_response": data, "status": "pending"}


async def _create_paytm(tx: dict, s: dict) -> dict:
    mid, merchant_key = s.get("mid"), s.get("merchant_key")
    if not mid or not merchant_key:
        raise GatewayError("Paytm credentials are incomplete")
    host = "https://securestage.paytmpayments.com" if s.get("mode", "test") == "test" else "https://secure.paytmpayments.com"
    body = {
        "requestType": "Payment",
        "mid": mid,
        "websiteName": s.get("website_name") or ("WEBSTAGING" if s.get("mode", "test") == "test" else "DEFAULT"),
        "orderId": tx["transaction_id"],
        "callbackUrl": f"{_base_url()}/webhooks/paytm/{tx['scope']}/{tx['owner_id']}",
        "txnAmount": {"value": f"{tx['amount']:.2f}", "currency": tx["currency"]},
        "userInfo": {"custId": str(tx["payer_user_id"])},
    }
    signature = PaytmChecksum.generateSignature(json.dumps(body, separators=(",", ":")), merchant_key)
    data = await _request("POST", f"{host}/theia/api/v1/initiateTransaction?mid={mid}&orderId={tx['transaction_id']}", headers={"Content-Type": "application/json"}, json={"body": body, "head": {"signature": signature}})
    txn_token = (data.get("body") or {}).get("txnToken")
    if not txn_token:
        raise GatewayError(f"Paytm transaction token missing: {data}")
    return {"gateway_order_id": tx["transaction_id"], "txn_token": txn_token, "checkout_url": f"{_base_url()}/checkout/paytm/{tx['transaction_id']}", "gateway_response": data, "status": "pending", "paytm_host": host, "paytm_mid": mid}


async def verify_and_fulfill_cashfree_return(transaction_id: str) -> tuple[bool, str]:
    """Verify a Cashfree order from the browser return route.

    Cashfree webhooks can be delayed or blocked by dashboard configuration.
    The return route therefore performs the same server-side verification and
    idempotent fulfillment without trusting browser query parameters.
    """
    tx = await get_gateway_transaction(str(transaction_id))
    if not tx or tx.get("gateway") != "cashfree":
        return False, "transaction not found"
    if tx.get("status") == "fulfilled" or tx.get("fulfilled_at") is not None:
        return True, "already fulfilled"

    cfg = await get_gateway_config(tx["scope"], tx["owner_id"], decrypt=True)
    settings = (cfg.get("gateways") or {}).get("cashfree") or {}
    try:
        payment_id, verified_payload = await _verify_cashfree_payment(tx, settings)
    except GatewayError as exc:
        await update_gateway_transaction(
            tx["transaction_id"],
            status="verification_pending",
            verification_error=str(exc)[:500],
            verification_checked_at=time.time(),
        )
        return False, str(exc)

    await claim_transaction_success(
        tx["transaction_id"],
        str(payment_id),
        {"source": "cashfree_return", "server_verification": verified_payload},
    )
    work = await claim_transaction_fulfillment(tx["transaction_id"])
    if not work:
        current = await get_gateway_transaction(tx["transaction_id"])
        if current and (current.get("status") == "fulfilled" or current.get("fulfilled_at") is not None):
            return True, "already fulfilled"
        return True, "already processing"
    try:
        await fulfill_transaction(work)
    except Exception as exc:
        await mark_transaction_fulfillment_retry(tx["transaction_id"], str(exc))
        raise
    return True, "fulfilled"


async def verify_and_process_webhook(gateway: str, scope: str, owner_id: int, headers: dict[str, str], raw_body: bytes, payload: dict) -> tuple[bool, str]:
    cfg = await get_gateway_config(scope, owner_id, decrypt=True)
    s = (cfg.get("gateways") or {}).get(gateway) or {}
    if gateway == "razorpay":
        secret = s.get("webhook_secret", "")
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, headers.get("x-razorpay-signature", "")):
            return False, "invalid signature"
        await mark_valid_webhook_received(scope, owner_id, "razorpay")
        event = payload.get("event", "")
        entity = (((payload.get("payload") or {}).get("payment") or {}).get("entity") or {})
        order = (((payload.get("payload") or {}).get("payment_link") or {}).get("entity") or {})
        txid = (entity.get("notes") or {}).get("transaction_id") or order.get("reference_id")
        success = event in {"payment.captured", "order.paid", "payment_link.paid"}
        payment_id = entity.get("id", "")
        if not payment_id and order.get("payments"):
            payment_id = order.get("payments", [{}])[-1].get("payment_id", "")
        event_key = payload.get("account_id", "") + ":" + event + ":" + str(entity.get("id") or order.get("id"))
    elif gateway == "cashfree":
        timestamp = headers.get("x-webhook-timestamp", "")
        signature = headers.get("x-webhook-signature", "")
        client_secret = str(s.get("client_secret", ""))
        if not client_secret or not timestamp or not signature:
            return False, "cashfree webhook credentials/headers missing"
        signed_payload = timestamp.encode("utf-8") + raw_body
        expected = base64.b64encode(
            hmac.new(client_secret.encode("utf-8"), signed_payload, hashlib.sha256).digest()
        ).decode("utf-8")
        if not hmac.compare_digest(expected, signature):
            return False, "invalid signature"
        data = payload.get("data") or {}
        order, payment = data.get("order") or {}, data.get("payment") or {}
        txid = order.get("order_id")
        payment_status = str(payment.get("payment_status", "")).upper()
        success = payment_status == "SUCCESS"
        payment_id = str(payment.get("cf_payment_id", ""))
        event_type = str(payload.get("type") or "PAYMENT_EVENT")
        event_key = (
            headers.get("x-idempotency-key")
            or f"{event_type}:{txid}:{payment_id}:{payment_status}"
            or hashlib.sha256(raw_body).hexdigest()
        )
    elif gateway == "phonepe":
        username = s.get("webhook_username", "")
        password = s.get("webhook_password", "")
        if not username or not password:
            return False, "webhook credentials not configured"
        expected = hashlib.sha256(f"{username}:{password}".encode()).hexdigest()
        if not hmac.compare_digest(expected.lower(), headers.get("authorization", "").replace("SHA256 ", "").lower()):
            return False, "invalid authorization"
        body = payload.get("payload") or payload
        txid = body.get("merchantOrderId") or ((body.get("metaInfo") or {}).get("udf1"))
        success = body.get("state") == "COMPLETED"
        payment_id = body.get("orderId", "")
        event_key = payload.get("event") or f"{txid}:{body.get('state')}"
    elif gateway == "paytm":
        checksum = payload.get("CHECKSUMHASH") or payload.get("checksumhash")
        params = {k: str(v) for k, v in payload.items() if k.upper() != "CHECKSUMHASH"}
        if not checksum or not PaytmChecksum.verifySignature(params, s.get("merchant_key", ""), checksum):
            return False, "invalid checksum"
        txid = payload.get("ORDERID") or payload.get("orderId")
        success = payload.get("STATUS") == "TXN_SUCCESS"
        payment_id = payload.get("TXNID", "")
        event_key = f"{txid}:{payment_id}:{payload.get('STATUS')}"
    else:
        return False, "unsupported gateway"

    if not txid:
        return False, "transaction not found"
    tx = await get_gateway_transaction(str(txid))
    if not tx:
        tx = await get_transaction_by_gateway_order(gateway, str(txid))
    if not tx:
        return False, "unknown transaction"
    if tx.get("gateway") != gateway:
        return False, "gateway mismatch"
    if tx.get("scope") != scope or int(tx.get("owner_id", -1)) != int(owner_id):
        return False, "payment scope mismatch"

    if gateway == "cashfree" and success:
        try:
            verified_payment_id, verified_payload = await _verify_cashfree_payment(tx, s)
        except GatewayError as exc:
            await update_gateway_transaction(
                tx["transaction_id"],
                status="verification_pending",
                verification_error=str(exc)[:500],
                last_gateway_event=payload,
            )
            return False, str(exc)
        payment_id = verified_payment_id
        payload = {**payload, "server_verification": verified_payload}

    fresh_event = await reserve_webhook_event(
        gateway,
        str(event_key),
        payload,
    )
    if not success:
        if fresh_event:
            if gateway == "cashfree":
                # Cashfree permits multiple payment attempts for one order. A
                # failed attempt must not permanently close the Telegram order.
                await update_gateway_transaction(
                    tx["transaction_id"],
                    status="pending",
                    last_gateway_failure=payload,
                    failure_reason="Cashfree payment attempt failed",
                )
            else:
                await mark_transaction_failed(
                    tx["transaction_id"],
                    "gateway reported failure",
                    payload,
                )
        return True, "failure attempt recorded"

    if tx.get("status") == "fulfilled":
        return True, "already processed"

    await claim_transaction_success(
        tx["transaction_id"],
        str(payment_id),
        payload,
    )

    work = await claim_transaction_fulfillment(tx["transaction_id"])
    if not work:
        current = await get_gateway_transaction(tx["transaction_id"])
        if current and current.get("status") == "fulfilled":
            return True, "already processed"
        return True, "already processing"

    try:
        await fulfill_transaction(work)
    except Exception as exc:
        await mark_transaction_fulfillment_retry(
            tx["transaction_id"],
            str(exc),
        )
        raise
    return True, "processed"


async def fulfill_transaction(tx: dict) -> None:
    if tx["purpose"] == "seller_plan":
        plan_id = tx["metadata"]["plan_id"]
        plan = await get_paid_plan(plan_id)
        if not plan:
            raise GatewayError("Seller plan no longer exists")
        purchase = await process_verified_plan_purchase(
            tx["payer_user_id"],
            plan_id,
            int(plan.get("duration_days", 30)),
            source=f"gateway:{tx['gateway']}",
            amount=tx["amount"],
            payment_reference=tx["transaction_id"],
            approved_by=0,
        )
        payment_record = {
            "payment_id": tx.get("gateway_payment_id") or tx["transaction_id"],
            "plan": plan.get("name", plan_id),
            "amount": tx["amount"],
        }
        invoice = await create_invoice(0, tx["payer_user_id"], payment_record, "Platform Owner")
        await audit(
            "seller_plan_gateway_paid",
            tx["payer_user_id"],
            0,
            {
                "transaction_id": tx["transaction_id"],
                "gateway": tx["gateway"],
                "plan_id": plan_id,
                "request_type": tx.get("metadata", {}).get("request_type", "upgrade"),
                "invoice_no": invoice.get("invoice_no"),
            },
        )
        fulfilled = await mark_transaction_fulfilled(
            tx["transaction_id"],
            {
                "plan_id": plan_id,
                "expiry_date": purchase.get("expiry_date"),
                "purchase_status": purchase.get("status"),
                "payment_id": purchase.get("payment_id"),
                "invoice_no": invoice.get("invoice_no"),
            },
        )
        if not fulfilled:
            return

        # All seller-plan messages are emitted here, after fulfillment is saved.
        # Separate atomic notification claims guarantee exactly one message for
        # each stage even when Razorpay sends payment.captured, order.paid and
        # payment_link.paid almost simultaneously.
        from keep_alive import send_runtime_message

        if await claim_transaction_notification(
            tx["transaction_id"], "seller_plan_payment_verified"
        ):
            try:
                duration_days = int(plan.get("duration_days", 30) or 30)
                status_line = (
                    "✅ Your seller plan has been extended successfully."
                    if purchase.get("decision") == "same_plan_extended"
                    else "✅ Your payment is secure. Please complete the plan decision below."
                )
                payment_text = (
                    "✅ Payment verified automatically\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 Plan Name: {plan.get('name', plan_id)}\n"
                    f"💰 Amount: ₹{float(tx.get('amount') or 0):g}\n"
                    f"💳 Gateway: {str(tx.get('gateway') or '').title() or '-'}\n"
                    f"🧾 Transaction ID: {tx.get('gateway_payment_id') or tx.get('transaction_id') or '-'}\n"
                    f"⌛ Duration: {duration_days} days\n"
                    f"🧾 Invoice: {invoice.get('invoice_no', '-')}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{status_line}"
                )
                await send_runtime_message(tx["payer_user_id"], payment_text)
                await complete_transaction_notification(
                    tx["transaction_id"],
                    "seller_plan_payment_verified",
                    {"purchase_status": purchase.get("status")},
                )
            except Exception as exc:
                await fail_transaction_notification(
                    tx["transaction_id"],
                    "seller_plan_payment_verified",
                    str(exc),
                )

        # A second message is needed only when the purchased plan differs from
        # the active plan. It is deliberately sent after the verified message.
        if purchase.get("status") == "decision_required" and await claim_transaction_notification(
            tx["transaction_id"], "seller_plan_change_detected"
        ):
            try:
                from handlers.seller import plan_change_text, plan_change_keyboard

                await send_runtime_message(
                    tx["payer_user_id"],
                    plan_change_text(purchase),
                    reply_markup=plan_change_keyboard(purchase["payment_id"]),
                )
                await complete_transaction_notification(
                    tx["transaction_id"],
                    "seller_plan_change_detected",
                    {"payment_id": purchase.get("payment_id")},
                )
            except Exception as exc:
                await fail_transaction_notification(
                    tx["transaction_id"],
                    "seller_plan_change_detected",
                    str(exc),
                )
        return
    if tx["purpose"] == "child_subscription":
        seller_id = tx["owner_id"]
        plan = await get_plan(seller_id, tx["metadata"]["plan_id"])
        if not plan:
            raise GatewayError("Child subscription plan no longer exists")
        payment = await create_automatic_payment(
            seller_id, tx["payer_user_id"], plan, tx["gateway"],
            tx["transaction_id"], tx.get("gateway_payment_id", ""),
        )

        # Only the first fulfillment attempt may add validity. A webhook or
        # recovery retry must never extend the same purchase a second time.
        if payment.get("_created_now"):
            expiry = await activate_subscription(
                seller_id, tx["payer_user_id"], plan["name"],
                plan["duration_minutes"], tx["amount"], plan.get("duration_text"),
            )
        else:
            existing_sub = await get_subscription(seller_id, tx["payer_user_id"])
            expiry = (existing_sub or {}).get("expiry_date")

        invoice = await create_invoice(seller_id, tx["payer_user_id"], payment, "Seller")
        await audit("child_gateway_payment_paid", tx["payer_user_id"], seller_id, {"transaction_id": tx["transaction_id"], "gateway": tx["gateway"], "invoice_no": invoice.get("invoice_no")})
        await mark_transaction_fulfilled(
            tx["transaction_id"],
            {"expiry_date": expiry, "invoice_no": invoice.get("invoice_no")},
        )

        # Fulfillment is already safely recorded. Invite delivery is best effort
        # and can be retried separately without charging or extending again.
        if await claim_transaction_notification(tx["transaction_id"], "subscriber_access"):
            try:
                from services.bot_manager import bot_manager
                delivery = await bot_manager.deliver_subscription_access(
                    seller_id,
                    tx["payer_user_id"],
                    success_details={
                        "plan_name": plan.get("name", "Subscription"),
                        "amount": tx.get("amount", 0),
                        "gateway": tx.get("gateway", ""),
                        "transaction_id": tx.get("transaction_id", ""),
                        "payment_date": tx.get("paid_at") or tx.get("updated_at") or tx.get("created_at"),
                        "expiry_date": expiry,
                        "duration": plan.get("duration_text") or f"{plan.get('duration_minutes', 0)} minutes",
                    },
                )
                if delivery.get("error") or (
                    int(delivery.get("sent", 0) or 0) == 0
                    and int(delivery.get("already_member", 0) or 0) == 0
                ):
                    raise GatewayError(
                        delivery.get("error")
                        or "No invite link was delivered and the user is not a member"
                    )
                await complete_transaction_notification(tx["transaction_id"], "subscriber_access", delivery)
                await audit(
                    "child_gateway_access_delivery",
                    tx["payer_user_id"],
                    seller_id,
                    {"transaction_id": tx["transaction_id"], **delivery},
                )
            except Exception as exc:
                await fail_transaction_notification(tx["transaction_id"], "subscriber_access", str(exc))
                await audit(
                    "child_gateway_access_delivery_failed",
                    tx["payer_user_id"],
                    seller_id,
                    {"transaction_id": tx["transaction_id"], "error": str(exc)[:500]},
                )

        # Notify the seller/admins inside the same clone bot. This is kept
        # separate from invite delivery, so the seller is notified even when
        # the subscriber has already joined every connected chat.
        if await claim_transaction_notification(tx["transaction_id"], "seller_notice"):
            try:
                from services.bot_manager import bot_manager
                notice = await bot_manager.notify_automatic_payment_success(
                    seller_id,
                    tx["payer_user_id"],
                    {
                        "plan_name": plan.get("name", "Subscription"),
                        "amount": tx.get("amount", 0),
                        "gateway": tx.get("gateway", ""),
                        "transaction_id": tx.get("gateway_payment_id") or tx.get("transaction_id", ""),
                        "payment_date": tx.get("paid_at") or tx.get("updated_at") or tx.get("created_at"),
                        "expiry_date": expiry,
                        "duration": plan.get("duration_text") or f"{plan.get('duration_minutes', 0)} minutes",
                        "invoice_no": invoice.get("invoice_no"),
                    },
                )
                await complete_transaction_notification(tx["transaction_id"], "seller_notice", notice)
                await audit(
                    "child_gateway_seller_notification",
                    tx["payer_user_id"],
                    seller_id,
                    {"transaction_id": tx["transaction_id"], **notice},
                )
            except Exception as exc:
                await fail_transaction_notification(tx["transaction_id"], "seller_notice", str(exc))
                await audit(
                    "child_gateway_seller_notification_failed",
                    tx["payer_user_id"],
                    seller_id,
                    {"transaction_id": tx["transaction_id"], "error": str(exc)[:500]},
                )
        return
    raise GatewayError("Unsupported transaction purpose")
