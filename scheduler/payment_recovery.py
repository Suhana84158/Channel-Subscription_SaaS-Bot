import logging

from database.payments import recover_orphaned_payments

logger = logging.getLogger(__name__)


async def recover_payments_job() -> None:
    """Periodic idempotent cleanup for incomplete payment records."""
    try:
        result = await recover_orphaned_payments()
    except Exception:
        logger.exception("Payment orphan recovery job failed")
        return

    changed = sum(int(value) for value in result.values())
    if changed:
        logger.warning(
            "[RECOVERY] Payment records repaired result=%s",
            result,
        )
    else:
        logger.debug("Payment recovery check completed; no repairs needed")
