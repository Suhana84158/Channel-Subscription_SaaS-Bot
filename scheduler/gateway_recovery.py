from __future__ import annotations

import logging

from database.payment_gateways import (
    claim_transaction_fulfillment,
    get_gateway_config,
    get_gateway_transaction,
    mark_transaction_fulfillment_retry,
    recoverable_gateway_transactions,
    update_gateway_transaction,
)
from services.payment_gateways import (
    GatewayError,
    _verify_cashfree_payment,
    fulfill_transaction,
)

logger = logging.getLogger(__name__)


async def recover_gateway_transactions_job() -> None:
    items = await recoverable_gateway_transactions(limit=50)
    for tx in items:
        transaction_id = str(tx.get("transaction_id") or "")
        if not transaction_id:
            continue
        try:
            current = await get_gateway_transaction(transaction_id)
            if not current or current.get("status") == "fulfilled":
                continue

            if current.get("status") == "verification_pending" and current.get("gateway") == "cashfree":
                cfg = await get_gateway_config(current["scope"], current["owner_id"], decrypt=True)
                settings = (cfg.get("gateways") or {}).get("cashfree") or {}
                payment_id, verified = await _verify_cashfree_payment(current, settings)
                await update_gateway_transaction(
                    transaction_id,
                    status="paid",
                    gateway_payment_id=payment_id,
                    server_verification=verified,
                    verification_error="",
                )

            work = await claim_transaction_fulfillment(transaction_id)
            if not work:
                continue
            try:
                await fulfill_transaction(work)
            except Exception as exc:
                await mark_transaction_fulfillment_retry(transaction_id, str(exc))
                raise
        except GatewayError as exc:
            logger.warning("Gateway recovery deferred transaction_id=%s error=%s", transaction_id, exc)
        except Exception:
            logger.exception("Gateway recovery failed transaction_id=%s", transaction_id)
