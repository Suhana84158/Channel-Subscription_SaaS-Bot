from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument, UpdateOne

BACKUP_FORMAT = "telegram-saas-backup"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 20 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_RECORDS = 50_000
RESTORE_LOCK_ID = "global_backup_restore"
RESTORE_LOCK_SECONDS = 30 * 60

ALLOWED_COLLECTIONS = (
    "sellers",
    "seller_bots",
    "seller_users",
    "seller_payments",
    "seller_subscriptions",
    "seller_invoices",
)

_IDENTITY_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "sellers": (("user_id",), ("owner_id",)),
    "seller_bots": (("bot_id",), ("owner_id", "bot_id")),
    "seller_users": (("owner_id", "user_id"), ("bot_id", "user_id")),
    "seller_payments": (("payment_id",), ("owner_id", "payment_id"), ("_id",)),
    "seller_subscriptions": (("subscription_id",), ("owner_id", "user_id", "channel_id"), ("_id",)),
    "seller_invoices": (("invoice_id",), ("owner_id", "invoice_id"), ("_id",)),
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return {"$date": value.astimezone(timezone.utc).isoformat()}
    try:
        from bson import ObjectId
        if isinstance(value, ObjectId):
            return {"$oid": str(value)}
    except ImportError:
        pass
    raise TypeError(f"Unsupported backup value: {type(value).__name__}")


def _object_hook(value: dict[str, Any]) -> Any:
    if set(value) == {"$date"}:
        try:
            parsed = datetime.fromisoformat(str(value["$date"]).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return value
    if set(value) == {"$oid"}:
        try:
            from bson import ObjectId
            return ObjectId(value["$oid"])
        except Exception:
            return value
    return value


async def create_backup(db) -> tuple[bytes, dict[str, Any]]:
    collections: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for name in ALLOWED_COLLECTIONS:
        docs: list[dict[str, Any]] = []
        async for document in db[name].find({}):
            docs.append(document)
            total += 1
            if total > MAX_RECORDS:
                raise ValueError(f"Backup exceeds {MAX_RECORDS:,} records")
        collections[name] = docs

    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": datetime.now(timezone.utc),
        "collections": collections,
    }
    canonical = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    checksum = hashlib.sha256(canonical).hexdigest()
    envelope = {
        "manifest": {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "created_at": payload["created_at"],
            "records": total,
            "sha256": checksum,
        },
        "payload": payload,
    }
    raw = json.dumps(envelope, default=_json_default, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9)
    if len(compressed) > MAX_BACKUP_BYTES:
        raise ValueError("Compressed backup is larger than the 20 MB safety limit")
    return compressed, envelope["manifest"]


def _safe_decompress(raw: bytes) -> bytes:
    """Decompress gzip data with a hard output limit to block gzip bombs."""
    if raw[:2] != b"\x1f\x8b":
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise ValueError("Backup JSON exceeds the decompressed safety limit")
        return raw

    output = io.BytesIO()
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DECOMPRESSED_BYTES:
                    raise ValueError("Decompressed backup exceeds the 100 MB safety limit")
                output.write(chunk)
    except (OSError, EOFError) as exc:
        raise ValueError("Invalid gzip backup") from exc
    return output.getvalue()


def parse_backup(raw: bytes) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if len(raw) > MAX_BACKUP_BYTES:
        raise ValueError("Backup file exceeds the 20 MB safety limit")
    decoded = _safe_decompress(raw)
    try:
        envelope = json.loads(decoded.decode("utf-8"), object_hook=_object_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid backup JSON") from exc

    if not isinstance(envelope, dict) or not isinstance(envelope.get("manifest"), dict):
        raise ValueError("Backup manifest is missing")
    manifest = envelope["manifest"]
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Backup payload is missing")
    if manifest.get("format") != BACKUP_FORMAT or payload.get("format") != BACKUP_FORMAT:
        raise ValueError("Unsupported backup format")
    if manifest.get("version") != BACKUP_VERSION or payload.get("version") != BACKUP_VERSION:
        raise ValueError("Unsupported backup version")

    canonical = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not hmac.compare_digest(
        hashlib.sha256(canonical).hexdigest(), str(manifest.get("sha256", ""))
    ):
        raise ValueError("Backup checksum verification failed")

    collections = payload.get("collections")
    if not isinstance(collections, dict):
        raise ValueError("Backup collections are missing")
    unknown = set(collections) - set(ALLOWED_COLLECTIONS)
    if unknown:
        raise ValueError(f"Unknown collections: {', '.join(sorted(unknown))}")

    total = 0
    validated: dict[str, list[dict[str, Any]]] = {}
    for name in ALLOWED_COLLECTIONS:
        records = collections.get(name, [])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError(f"Invalid records in collection {name}")
        total += len(records)
        if total > MAX_RECORDS:
            raise ValueError(f"Backup exceeds {MAX_RECORDS:,} records")
        validated[name] = records
    if int(manifest.get("records", -1)) != total:
        raise ValueError("Backup record count does not match manifest")
    return validated, manifest


def _identity_filter(collection: str, document: dict[str, Any]) -> dict[str, Any] | None:
    for fields in _IDENTITY_FIELDS[collection]:
        if all(document.get(field) is not None for field in fields):
            return {field: document[field] for field in fields}
    return None


async def _claim_restore_lock(db, actor_id: int) -> str | None:
    """Allow only one restore at a time across all bot processes."""
    from datetime import timedelta
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    token = uuid4().hex
    await db["backup_restore_locks"].create_index("expires_at", expireAfterSeconds=0)
    claimed = await db["backup_restore_locks"].find_one_and_update(
        {
            "_id": RESTORE_LOCK_ID,
            "$or": [
                {"expires_at": {"$lte": now}},
                {"expires_at": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "token": token,
                "actor_id": int(actor_id),
                "claimed_at": now,
                "expires_at": now + timedelta(seconds=RESTORE_LOCK_SECONDS),
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return token if claimed and claimed.get("token") == token else None


async def _release_restore_lock(db, token: str) -> None:
    await db["backup_restore_locks"].delete_one(
        {"_id": RESTORE_LOCK_ID, "token": token}
    )


async def restore_backup(db, raw: bytes, *, actor_id: int = 0) -> dict[str, int]:
    collections, manifest = parse_backup(raw)
    token = await _claim_restore_lock(db, actor_id)
    if not token:
        raise ValueError("Another backup restore is already running")

    result = {
        "records": int(manifest["records"]),
        "inserted": 0,
        "existing": 0,
        "skipped": 0,
    }
    try:
        # Restore is intentionally non-destructive. Existing records are left
        # untouched; only records missing from the live database are inserted.
        # This prevents an old backup from silently overwriting newer payments,
        # subscriptions, tokens, or seller settings.
        for name, records in collections.items():
            operations = []
            seen: set[str] = set()
            for document in records:
                identity = _identity_filter(name, document)
                if identity is None:
                    result["skipped"] += 1
                    continue

                identity_key = json.dumps(
                    identity, default=_json_default, sort_keys=True, separators=(",", ":")
                )
                if identity_key in seen:
                    result["skipped"] += 1
                    continue
                seen.add(identity_key)
                operations.append(
                    UpdateOne(identity, {"$setOnInsert": document}, upsert=True)
                )

            if operations:
                write = await db[name].bulk_write(operations, ordered=False)
                inserted = int(write.upserted_count or 0)
                result["inserted"] += inserted
                result["existing"] += len(operations) - inserted
        return result
    finally:
        await _release_restore_lock(db, token)
