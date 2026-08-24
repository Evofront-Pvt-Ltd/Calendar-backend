import re
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def object_id(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise ValueError("Invalid object id")
    return ObjectId(value)


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def public_doc(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    result: dict[str, Any] = {}
    for key, value in document.items():
        if key in {"password_hash", "otp_hash", "google_sub"}:
            continue
        if key == "_id":
            result["id"] = str(value)
        elif isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = as_utc(value).isoformat().replace("+00:00", "Z")
        elif isinstance(value, list):
            result[key] = [public_doc(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            result[key] = public_doc(value)
        else:
            result[key] = value
    return result


async def unique_slug(collection: Any, base: str, query: dict[str, Any] | None = None) -> str:
    root = slugify(base)
    slug = root
    index = 2
    query = query or {}
    while await collection.find_one({**query, "slug": slug}):
        slug = f"{root}-{index}"
        index += 1
    return slug
