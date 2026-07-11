from datetime import datetime, timezone

from bson import ObjectId
from database.mongo import get_database

COLLECTION = "payments"


def payments_collection():
    return get_database()[COLLECTION]


async def create_payment(
    user_id: int,
    plan: str,
    amount: float,
    screenshot_file_id: str = None,
    utr: str = None,
    duration_minutes: int = None,
    duration_text: str = None,
):
    now = datetime.now(timezone.utc)

    payment = {
        "user_id": user_id,
        "plan": plan,
        "amount": amount,
        "screenshot_file_id": screenshot_file_id,
        "utr": utr,
        "duration_minutes": duration_minutes,
        "duration_text": duration_text,
        "status": "pending",
        "admin_id": None,
        "remarks": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await payments_collection().insert_one(payment)
    payment["_id"] = result.inserted_id
    return payment


def to_object_id(payment_id):
    if isinstance(payment_id, ObjectId):
        return payment_id
    return ObjectId(str(payment_id))


async def get_payment(payment_id):
    return await payments_collection().find_one(
        {"_id": to_object_id(payment_id)}
    )


async def get_pending_payments(limit: int = 20):
    return await payments_collection().find(
        {"status": "pending"}
    ).sort("created_at", -1).to_list(length=limit)


async def get_payment_history(limit: int = 50):
    return await payments_collection().find(
        {"status": {"$in": ["approved", "rejected"]}}
    ).sort("updated_at", -1).to_list(length=limit)


async def update_payment_status(
    user_id: int,
    status: str,
    admin_id: int = None,
    remarks: str = None,
):
    payment = await payments_collection().find_one(
        {"user_id": user_id, "status": "pending"},
        sort=[("created_at", -1)],
    )

    if not payment:
        return False

    await payments_collection().update_one(
        {"_id": payment["_id"]},
        {
            "$set": {
                "status": status,
                "admin_id": admin_id,
                "remarks": remarks,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    return True


async def update_payment_status_by_id(
    payment_id,
    status: str,
    admin_id: int = None,
    remarks: str = None,
):
    result = await payments_collection().update_one(
        {"_id": to_object_id(payment_id)},
        {
            "$set": {
                "status": status,
                "admin_id": admin_id,
                "remarks": remarks,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    return result.modified_count > 0


async def approve_payment(payment_id, admin_id: int):
    return await update_payment_status_by_id(
        payment_id=payment_id,
        status="approved",
        admin_id=admin_id,
    )


async def reject_payment(payment_id, admin_id: int, remarks: str = ""):
    return await update_payment_status_by_id(
        payment_id=payment_id,
        status="rejected",
        admin_id=admin_id,
        remarks=remarks,
    )


async def count_pending_payments():
    return await payments_collection().count_documents(
        {"status": "pending"}
    )


async def total_revenue():
    pipeline = [
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]

    result = await payments_collection().aggregate(
        pipeline
    ).to_list(length=1)

    return result[0]["total"] if result else 0


async def total_payments():
    return await payments_collection().count_documents({})
