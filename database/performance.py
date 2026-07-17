from __future__ import annotations

import time
from database.mongo import get_database


async def initialize_performance_indexes() -> None:
    db = get_database()
    indexes = {
        "users": [("user_id", 1), ("username", 1), ("banned", 1), ("joined_at", -1)],
        "payments": [("status", 1), ("user_id", 1), ("created_at", -1), ("updated_at", -1)],
        "subscriptions": [("user_id", 1), ("active", 1), ("expiry_date", 1)],
        "seller_users": [("owner_id", 1), ("user_id", 1), ("banned", 1)],
        "seller_payments": [("owner_id", 1), ("status", 1), ("created_at", -1)],
        "seller_subscriptions": [("owner_id", 1), ("user_id", 1), ("active", 1), ("expiry_date", 1)],
        "seller_channels": [("owner_id", 1), ("chat_id", 1)],
        "seller_plans": [("owner_id", 1), ("active", 1)],
        "seller_bots": [("owner_id", 1), ("active", 1), ("runtime_status", 1)],
    }
    for collection_name, specs in indexes.items():
        collection = db[collection_name]
        for field, direction in specs:
            await collection.create_index([(field, direction)], background=True)


async def database_ping_ms() -> float:
    db = get_database()
    started = time.perf_counter()
    await db.command("ping")
    return (time.perf_counter() - started) * 1000.0
