from __future__ import annotations

import logging

from database.payment_gateways import (
    claim_transaction_fulfillment,
    get_gateway_config,
    get_gateway_transaction,
    mark_transaction_fulfillment_retry,
    recoverable_gateway_transactions,
    recoverable_failed_notifications,
    reclaim_failed_transaction_notification,
    complete_transaction_notification,
    fail_transaction_notification,
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
            if (
                not current
                or current.get("status") == "fulfilled"
                or current.get("fulfilled_at") is not None
            ):
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


async def recover_failed_invite_deliveries_job() -> None:
    """Retry only failed subscriber invite delivery for already-fulfilled payments."""
    items = await recoverable_failed_notifications(limit=50)
    for tx in items:
        transaction_id = str(tx.get("transaction_id") or "")
        if not transaction_id:
            continue
        try:
            claimed = await reclaim_failed_transaction_notification(
                transaction_id,
                "subscriber_access",
            )
            if not claimed:
                continue

            from database.seller_data import get_plan, get_subscription
            from services.bot_manager import bot_manager

            seller_id = int(tx.get("owner_id") or 0)
            user_id = int(tx.get("payer_user_id") or 0)
            plan_id = str((tx.get("metadata") or {}).get("plan_id") or "")
            plan = await get_plan(seller_id, plan_id)
            if not plan:
                raise GatewayError("Child subscription plan no longer exists")

            fulfillment = tx.get("fulfillment") or {}
            expiry = fulfillment.get("expiry_date")
            if expiry is None:
                sub = await get_subscription(seller_id, user_id)
                expiry = (sub or {}).get("expiry_date")

            delivery = await bot_manager.deliver_subscription_access(
                seller_id,
                user_id,
                success_details={
                    "plan_name": plan.get("name", "Subscription"),
                    "amount": tx.get("amount", 0),
                    "gateway": tx.get("gateway", ""),
                    "transaction_id": tx.get("gateway_payment_id") or transaction_id,
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

            await complete_transaction_notification(
                transaction_id,
                "subscriber_access",
                delivery,
            )
            logger.info(
                "Recovered invite delivery transaction_id=%s sent=%s already_member=%s",
                transaction_id,
                delivery.get("sent", 0),
                delivery.get("already_member", 0),
            )
        except Exception as exc:
            await fail_transaction_notification(
                transaction_id,
                "subscriber_access",
                str(exc),
            )
            logger.exception(
                "Invite delivery recovery failed transaction_id=%s",
                transaction_id,
            )
