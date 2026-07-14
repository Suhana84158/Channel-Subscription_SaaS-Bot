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
    claim_transaction_success,
    get_gateway_config,
    get_gateway_transaction,
    get_transaction_by_gateway_order,
    mark_transaction_failed,
    mark_transaction_fulfilled,
    reserve_webhook_event,
    update_gateway_transaction,
)
from database.seller_data import activate_subscription, get_plan, create_automatic_payment
from database.seller_subscriptions import assign_plan_with_history, get_paid_plan
from database.platform_features import create_invoice, audit


class GatewayError(RuntimeError):
    pass


def _base_url() -> str:
    value = (PUBLIC_BASE_URL or "").rstrip("/")
    if not value:
        raise GatewayError("PUBLIC_BASE_URL is not configured")
    return value


async def _request(method: str, url: str, **kwargs) -> dict:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, **kwargs) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"raw": text}
            if response.status >= 400:
                raise GatewayError(f"Gateway HTTP {response.status}: {data}")
            return data




async def test_gateway_connection(scope: str, owner_id: int, gateway: str) -> dict:
    """Validate stored credentials without creating or charging a payment."""
    cfg = await get_gateway_config(scope, owner_id, decrypt=True)
    settings = (cfg.get("gateways") or {}).get(gateway) or {}
    mode = settings.get("mode", "test")
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
    raise GatewayError(f"Connection test for {gateway.title()} will be added in its gateway patch")


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
    client_id, client_secret = s.get("client_id"), s.get("client_secret")
    if not client_id or not client_secret:
        raise GatewayError("Cashfree credentials are incomplete")
    base = "https://sandbox.cashfree.com/pg" if s.get("mode", "test") == "test" else "https://api.cashfree.com/pg"
    payload = {
        "order_id": tx["transaction_id"],
        "order_amount": tx["amount"],
        "order_currency": tx["currency"],
        "customer_details": {
            "customer_id": str(tx["payer_user_id"]),
            "customer_phone": tx["metadata"].get("phone", "9999999999"),
            "customer_email": tx["metadata"].get("email", f"telegram{tx['payer_user_id']}@example.com"),
        },
        "order_meta": {
            "return_url": f"{_base_url()}/payment/return/{tx['transaction_id']}",
            "notify_url": f"{_base_url()}/webhooks/cashfree/{tx['scope']}/{tx['owner_id']}",
        },
        "order_note": tx["metadata"].get("description", tx["purpose"]),
    }
    data = await _request("POST", f"{base}/orders", headers={"x-client-id": client_id, "x-client-secret": client_secret, "x-api-version": "2025-01-01", "Content-Type": "application/json", "x-idempotency-key": tx["transaction_id"]}, json=payload)
    checkout = f"{_base_url()}/checkout/cashfree/{tx['transaction_id']}"
    return {"gateway_order_id": data.get("cf_order_id", tx["transaction_id"]), "payment_session_id": data.get("payment_session_id", ""), "checkout_url": checkout, "gateway_response": data, "gateway_mode": s.get("mode", "test"), "status": "pending"}


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


async def verify_and_process_webhook(gateway: str, scope: str, owner_id: int, headers: dict[str, str], raw_body: bytes, payload: dict) -> tuple[bool, str]:
    cfg = await get_gateway_config(scope, owner_id, decrypt=True)
    s = (cfg.get("gateways") or {}).get(gateway) or {}
    if gateway == "razorpay":
        secret = s.get("webhook_secret", "")
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, headers.get("x-razorpay-signature", "")):
            return False, "invalid signature"
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
        expected = base64.b64encode(hmac.new(str(s.get("client_secret", "")).encode(), timestamp.encode() + raw_body, hashlib.sha256).digest()).decode()
        if not signature or not hmac.compare_digest(expected, signature):
            return False, "invalid signature"
        data = payload.get("data") or {}
        order, payment = data.get("order") or {}, data.get("payment") or {}
        txid = order.get("order_id")
        success = payment.get("payment_status") == "SUCCESS"
        payment_id = str(payment.get("cf_payment_id", ""))
        event_key = headers.get("x-idempotency-key") or payment_id or hashlib.sha256(raw_body).hexdigest()
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
    fresh_event = await reserve_webhook_event(gateway, str(event_key), payload)
    if not success:
        if fresh_event:
            await mark_transaction_failed(tx["transaction_id"], "gateway reported failure", payload)
        return True, "failure recorded"
    if tx.get("status") == "fulfilled":
        return True, "already processed"
    claimed = await claim_transaction_success(tx["transaction_id"], str(payment_id), payload)
    work = claimed or tx
    if work.get("status") not in {"paid", "paid_unfulfilled"}:
        return True, "already processing"
    try:
        await fulfill_transaction(work)
    except Exception as exc:
        await update_gateway_transaction(tx["transaction_id"], status="paid_unfulfilled", fulfillment_error=str(exc)[:500])
        raise
    return True, "processed"


async def fulfill_transaction(tx: dict) -> None:
    if tx["purpose"] == "seller_plan":
        plan_id = tx["metadata"]["plan_id"]
        plan = await get_paid_plan(plan_id)
        if not plan:
            raise GatewayError("Seller plan no longer exists")
        assignment = await assign_plan_with_history(
            tx["payer_user_id"],
            plan_id,
            int(plan.get("duration_days", 30)),
            f"gateway:{tx['gateway']}",
            tx["amount"],
            0,
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
        await mark_transaction_fulfilled(
            tx["transaction_id"],
            {
                "plan_id": plan_id,
                "expiry_date": assignment.get("expiry_date"),
                "invoice_no": invoice.get("invoice_no"),
            },
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
        sub = await activate_subscription(
            seller_id, tx["payer_user_id"], plan["name"],
            plan["duration_minutes"], tx["amount"], plan.get("duration_text"),
        )
        invoice = await create_invoice(seller_id, tx["payer_user_id"], payment, "Seller")
        await audit("child_gateway_payment_paid", tx["payer_user_id"], seller_id, {"transaction_id": tx["transaction_id"], "gateway": tx["gateway"], "invoice_no": invoice.get("invoice_no")})
        await mark_transaction_fulfilled(tx["transaction_id"], {"expiry_date": sub.get("expiry_date"), "invoice_no": invoice.get("invoice_no")})
        return
    raise GatewayError("Unsupported transaction purpose")
