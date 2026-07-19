from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from zoneinfo import ZoneInfo
from telegram.ext import CallbackQueryHandler, ContextTypes

from database.admins import is_admin
from database.payments import (
    update_payment_status,
    update_payment_status_by_id,
    decide_latest_payment,
    decide_payment_by_id,
    get_pending_payments,
    get_payment,
    get_payment_history,
    get_latest_payment_for_user,
    claim_payment_fulfillment,
    mark_payment_subscription_fulfilled,
    complete_payment_fulfillment,
    fail_payment_fulfillment,
    claim_payment_user_notification,
    complete_payment_user_notification,
    fail_payment_user_notification,
)
from services.subscription_service import fulfill_payment_subscription
from services.channel_service import grant_channel_access


def format_ist(dt):
    return dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime(
        "%d-%m-%Y %I:%M:%S %p IST"
    )


async def safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_caption(caption=text, reply_markup=reply_markup)
    except Exception:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            pass


async def _send_approval_notification(
    payment,
    *,
    context: ContextTypes.DEFAULT_TYPE,
):
    payment_id = str(payment["_id"])
    claimed = await claim_payment_user_notification(payment_id)
    if not claimed:
        current = await get_payment(payment_id)
        return not current or current.get("user_notification_status") == "sent"

    user_id = int(claimed["user_id"])
    plan_name = claimed.get("plan", "Premium")
    expiry = claimed.get("subscription_expiry")
    expiry_text = format_ist(expiry) if expiry else "Active"
    action = claimed.get("subscription_action", "activated")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 Payment Approved!\n\n"
                f"Plan: {plan_name}\n"
                f"Subscription {action}.\n"
                f"Expiry: {expiry_text}"
            ),
        )
        await complete_payment_user_notification(payment_id)
        return True
    except Exception as exc:
        await fail_payment_user_notification(payment_id, str(exc))
        return False


async def fulfill_approved_payment(
    payment,
    *,
    admin_id: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    payment_id = str(payment["_id"])

    claimed = await claim_payment_fulfillment(
        payment_id,
        admin_id=admin_id,
    )
    if not claimed:
        current = await get_payment(payment_id)
        if current and current.get("fulfillment_status") == "completed":
            notified = await _send_approval_notification(
                current,
                context=context,
            )
            return {
                "completed": True,
                "already_completed": True,
                "notification_sent": notified,
                "payment": current,
            }
        return {
            "completed": False,
            "already_processing": True,
            "payment": current,
        }

    user_id = int(claimed["user_id"])
    plan_name = claimed.get("plan", "Premium")
    duration_minutes = int(claimed.get("duration_minutes") or 43200)
    plan_days = duration_minutes // 1440 if duration_minutes % 1440 == 0 else 0

    try:
        fulfillment = await fulfill_payment_subscription(
            user_id=user_id,
            fulfillment_key=f"manual-payment:{payment_id}",
            plan_name=plan_name,
            plan_days=plan_days,
            duration_minutes=duration_minutes,
        )
        expiry = fulfillment["expiry"]
        action = fulfillment["action"]

        saved = await mark_payment_subscription_fulfilled(
            payment_id,
            expiry=expiry,
            action=action,
        )
        if not saved:
            raise RuntimeError("Subscription fulfillment result could not be saved.")

        access_result = await grant_channel_access(
            user_id,
            payment_id=payment_id,
            already_delivered_chat_ids=claimed.get("fulfilled_channel_ids", []),
        )
        if access_result["failed"]:
            raise RuntimeError(
                "Channel delivery failed: "
                + ", ".join(str(item["chat_id"]) for item in access_result["failed"])
            )

        completed = await complete_payment_fulfillment(payment_id)
        if not completed:
            raise RuntimeError("Payment fulfillment completion could not be saved.")

        current = await get_payment(payment_id)
        notified = await _send_approval_notification(
            current or claimed,
            context=context,
        )
        expiry_ist = format_ist(expiry)

        return {
            "completed": True,
            "already_completed": False,
            "notification_sent": notified,
            "user_id": user_id,
            "plan_name": plan_name,
            "expiry": expiry,
            "expiry_ist": expiry_ist,
            "action": action,
        }

    except Exception as exc:
        await fail_payment_fulfillment(payment_id, str(exc))
        raise


async def show_pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Not authorized")
        return

    payments = await get_pending_payments(limit=10)

    if not payments:
        await query.edit_message_text("📨 No pending payments.")
        return

    text = "📨 Pending Payments\n\n"
    keyboard = []

    for payment in payments:
        payment_id = str(payment["_id"])
        user_id = payment.get("user_id")
        amount = payment.get("amount")
        plan = payment.get("plan", "Plan")

        text += f"• User: {user_id} | ₹{amount} | {plan}\n"

        keyboard.append([
            InlineKeyboardButton(
                f"View ₹{amount} - {user_id}",
                callback_data=f"pay_view_{payment_id}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("⬅ Back", callback_data="main_owner_dashboard")
    ])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Not authorized")
        return

    payments = await get_payment_history(limit=20)

    if not payments:
        await query.edit_message_text(
            "📜 No payment history found."
        )
        return

    text = "📜 Payment History\n\n"

    for payment in payments:
        status = "✅" if payment["status"] == "approved" else "❌"

        text += (
            f"{status} User: {payment['user_id']}\n"
            f"💰 ₹{payment['amount']}\n"
            f"📦 {payment['plan']}\n"
            f"⏳ {payment.get('duration_text', '-')}\n\n"
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅ Back",
                callback_data="main_owner_dashboard",
            )
        ]
    ])

    await query.edit_message_text(
        text,
        reply_markup=keyboard,
    )
    
async def view_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Not authorized")
        return

    payment_id = query.data.replace("pay_view_", "")
    payment = await get_payment(payment_id)

    if not payment:
        await query.edit_message_text("❌ Payment not found.")
        return

    user_id = payment.get("user_id")
    amount = payment.get("amount")
    plan = payment.get("plan", "Plan")
    duration_text = payment.get("duration_text", "-")
    screenshot = payment.get("screenshot_file_id")

    caption = (
        "🧾 Pending Payment\n\n"
        f"👤 User ID: {user_id}\n"
        f"📦 Plan: {plan}\n"
        f"💰 Amount: ₹{amount}\n"
        f"⏳ Duration: {duration_text}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"pay_approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"pay_reject_{payment_id}"),
        ],
        [InlineKeyboardButton("⬅ Pending List", callback_data="admin_pending_payments")],
    ])

    if screenshot:
        await query.message.reply_photo(
            photo=screenshot,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        await query.message.reply_text(
            caption,
            reply_markup=keyboard,
        )


async def approve_payment_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await safe_edit(query, "❌ Not authorized")
        return

    payment_id = query.data.replace("pay_approve_", "")
    payment = await get_payment(payment_id)

    if not payment:
        await safe_edit(query, "❌ Payment not found.")
        return

    try:
        if payment.get("status") == "pending":
            decided = await decide_payment_by_id(
                payment_id=payment_id,
                status="approved",
                admin_id=query.from_user.id,
            )
            if decided is None:
                payment = await get_payment(payment_id)
                winner = payment.get("status") if payment else "unknown"
                await safe_edit(
                    query,
                    "⚠️ Payment was already decided by another admin.\n\n"
                    f"Final status: {winner}",
                )
                return
            payment = decided
        elif payment.get("status") != "approved":
            await safe_edit(
                query,
                "⚠️ Payment was already decided.\n\n"
                f"Final status: {payment.get('status', 'unknown')}",
            )
            return
        result = await fulfill_approved_payment(
            payment,
            admin_id=query.from_user.id,
            context=context,
        )

        if result.get("already_completed"):
            text = "✅ Payment was already approved and delivered."
            if not result.get("notification_sent", True):
                text += "\n\n⚠️ User notification could not be sent."
            await safe_edit(query, text)
            return

        if result.get("already_processing"):
            await safe_edit(
                query,
                "⏳ Payment fulfillment is already processing.",
            )
            return

        success_text = (
            "✅ Payment Approved\n\n"
            f"User: {result['user_id']}\n"
            f"Plan: {result['plan_name']}\n"
            f"Expiry: {result['expiry_ist']}"
        )
        if not result.get("notification_sent", True):
            success_text += "\n\n⚠️ User notification could not be sent."
        await safe_edit(query, success_text)

    except Exception as exc:
        retry_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔁 Retry Delivery",
                    callback_data=f"pay_approve_{payment_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅ Payment History",
                    callback_data="admin_payment_history",
                )
            ],
        ])
        await safe_edit(
            query,
            "⚠️ Payment approved, but delivery is incomplete.\\n\\n"
            f"Error: {exc}",
            reply_markup=retry_keyboard,
        )


async def reject_payment_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await safe_edit(query, "❌ Not authorized")
        return

    try:
        payment_id = query.data.replace("pay_reject_", "")
        payment = await get_payment(payment_id)

        if not payment:
            await safe_edit(query, "❌ Payment not found.")
            return

        user_id = payment["user_id"]

        decided = await decide_payment_by_id(
            payment_id=payment_id,
            status="rejected",
            admin_id=query.from_user.id,
        )

        if decided is None:
            current = await get_payment(payment_id)
            final_status = (
                current.get("status", "unknown")
                if current
                else "unknown"
            )
            await safe_edit(
                query,
                "⚠️ Payment was already decided by another admin.\n\n"
                f"Final status: {final_status}",
            )
            return

        await safe_edit(query, "❌ Payment Rejected")

        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Your payment was rejected.",
        )

    except Exception as e:
        await safe_edit(query, str(e))


async def approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await safe_edit(query, "❌ Not authorized")
        return

    try:
        data = query.data.split("_")
        user_id = int(data[1])

        payment = await decide_latest_payment(
            user_id=user_id,
            status="approved",
            admin_id=query.from_user.id,
        )

        if payment is None:
            latest = await get_latest_payment_for_user(user_id)
            final_status = (
                latest.get("status", "unknown")
                if latest
                else "unknown"
            )
            await safe_edit(
                query,
                "⚠️ Payment was already decided.\n\n"
                f"Final status: {final_status}",
            )
            return

        result = await fulfill_approved_payment(
            payment,
            admin_id=query.from_user.id,
            context=context,
        )

        if result.get("already_completed"):
            text = "✅ Payment was already approved and delivered."
            if not result.get("notification_sent", True):
                text += "\n\n⚠️ User notification could not be sent."
            await safe_edit(query, text)
            return

        if result.get("already_processing"):
            await safe_edit(
                query,
                "⏳ Payment fulfillment is already processing.",
            )
            return

        success_text = (
            "✅ Payment Approved\n\n"
            f"User: {result['user_id']}\n"
            f"Plan: {result['plan_name']}\n"
            f"Expiry: {result['expiry_ist']}"
        )
        if not result.get("notification_sent", True):
            success_text += "\n\n⚠️ User notification could not be sent."
        await safe_edit(query, success_text)

    except Exception as exc:
        await safe_edit(
            query,
            "⚠️ Payment approved, but delivery is incomplete.\\n\\n"
            f"Error: {exc}",
        )


async def reject_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await safe_edit(query, "❌ Not authorized")
        return

    try:
        user_id = int(query.data.split("_")[1])

        decided = await decide_latest_payment(
            user_id=user_id,
            status="rejected",
            admin_id=query.from_user.id,
        )

        if decided is None:
            latest = await get_latest_payment_for_user(user_id)
            final_status = (
                latest.get("status", "unknown")
                if latest
                else "unknown"
            )
            await safe_edit(
                query,
                "⚠️ Payment was already decided.\n\n"
                f"Final status: {final_status}",
            )
            return

        await safe_edit(query, "❌ Payment Rejected")

        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Your payment was rejected.",
        )

    except Exception as e:
        await safe_edit(query, str(e))


def payment_approval_handlers():
    return [
        CallbackQueryHandler(show_payment_history, pattern=r"^admin_payment_history$",),
        CallbackQueryHandler(show_pending_payments, pattern=r"^admin_pending_payments$"),
        CallbackQueryHandler(view_payment, pattern=r"^pay_view_"),
        CallbackQueryHandler(approve_payment_by_id, pattern=r"^pay_approve_"),
        CallbackQueryHandler(reject_payment_by_id, pattern=r"^pay_reject_"),
        CallbackQueryHandler(approve_payment, pattern=r"^approve_"),
        CallbackQueryHandler(reject_payment, pattern=r"^reject_"),
    ]
