from datetime import datetime, timezone

from database.mongo import get_database

COLLECTION = "referrals"


def referrals_collection():
    return get_database()[COLLECTION]


async def create_referral(
    referrer_id: int,
    referred_id: int,
):
    document = {
        "referrer_id": referrer_id,
        "referred_id": referred_id,
        "bonus_given": False,
        "created_at": datetime.now(timezone.utc),
    }

    await referrals_collection().insert_one(document)

    return document


async def referral_exists(referred_id: int):
    return await referrals_collection().find_one(
        {"referred_id": referred_id}
    )


async def get_referrals(referrer_id: int):
    return await referrals_collection().find(
        {"referrer_id": referrer_id}
    ).to_list(length=None)


async def total_referrals(referrer_id: int):
    return await referrals_collection().count_documents(
        {"referrer_id": referrer_id}
    )


async def mark_bonus_given(referred_id: int):
    await referrals_collection().update_one(
        {"referred_id": referred_id},
        {
            "$set": {
                "bonus_given": True,
            }
        },
    )
