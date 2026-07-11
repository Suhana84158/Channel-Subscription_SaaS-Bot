from database.mongo import get_database

COLLECTION = "channels"


def channels_collection():
    return get_database()[COLLECTION]


async def add_channel(chat_id: int, title: str, plans: list = None):
    if plans is None:
        plans = []

    await channels_collection().update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "chat_id": chat_id,
                "title": title,
                "plans": plans,
            }
        },
        upsert=True,
    )


async def update_channel_plans(chat_id: int, plans: list):
    await channels_collection().update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "plans": plans,
            }
        },
    )


async def remove_channel(chat_id: int):
    await channels_collection().delete_one(
        {"chat_id": chat_id}
    )


async def get_channel(chat_id: int):
    return await channels_collection().find_one(
        {"chat_id": chat_id}
    )


async def get_all_channels():
    return await channels_collection().find({}).to_list(length=None)


async def total_channels():
    return await channels_collection().count_documents({})
