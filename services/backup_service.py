from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pymongo import ReplaceOne

BACKUP_FORMAT = "telegram-saas-backup"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 20 * 1024 * 1024
MAX_RECORDS = 50_000

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


def parse_backup(raw: bytes) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if len(raw) > MAX_BACKUP_BYTES:
        raise ValueError("Backup file exceeds the 20 MB safety limit")
    try:
        decoded = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    except OSError as exc:
        raise ValueError("Invalid gzip backup") from exc
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
    if not hashlib.compare_digest(
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


async def restore_backup(db, raw: bytes) -> dict[str, int]:
    collections, manifest = parse_backup(raw)
    result = {"records": int(manifest["records"]), "restored": 0, "skipped": 0}
    # Validation above completes before the first database write. Restore is
    # non-destructive and idempotent: it never deletes current records.
    for name, records in collections.items():
        operations = []
        for document in records:
            identity = _identity_filter(name, document)
            if identity is None:
                result["skipped"] += 1
                continue
            operations.append(ReplaceOne(identity, document, upsert=True))
        if operations:
            write = await db[name].bulk_write(operations, ordered=False)
            result["restored"] += write.matched_count + write.upserted_count
    return result
