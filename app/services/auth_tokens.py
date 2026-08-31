"""Opaque refresh tokens for session renewal.

Refresh tokens are random strings, never JWTs: only their SHA-256 hash is stored, so a
database leak cannot be replayed as a session. Every use rotates the token, and replaying a
token that was already rotated revokes the whole user's sessions as a theft signal.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import as_utc, now_utc

REFRESH_TOKEN_BYTES = 48


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_lifetime() -> timedelta:
    return timedelta(days=settings.refresh_token_expire_days)


async def issue_refresh_token(user_id: str) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    issued_at = now_utc()
    expires_at = issued_at + refresh_token_lifetime()
    await get_database().refresh_tokens.insert_one(
        {
            "user_id": str(user_id),
            "token_hash": hash_refresh_token(token),
            "created_at": issued_at,
            "expires_at": expires_at,
            "revoked_at": None,
        }
    )
    return token, expires_at


async def revoke_all_for_user(user_id: str) -> int:
    result = await get_database().refresh_tokens.update_many(
        {"user_id": str(user_id), "revoked_at": None},
        {"$set": {"revoked_at": now_utc()}},
    )
    return result.modified_count


async def revoke_refresh_token(token: str) -> bool:
    result = await get_database().refresh_tokens.update_one(
        {"token_hash": hash_refresh_token(token), "revoked_at": None},
        {"$set": {"revoked_at": now_utc()}},
    )
    return result.modified_count > 0


async def rotate_refresh_token(token: str) -> tuple[str, str, datetime] | None:
    """Consume a refresh token and return (user_id, new_token, expires_at), or None if invalid."""
    db = get_database()
    record = await db.refresh_tokens.find_one({"token_hash": hash_refresh_token(token)})
    if record is None:
        return None

    user_id = str(record.get("user_id", ""))
    if record.get("revoked_at") is not None:
        # Replay of an already-rotated token: assume the token family is compromised.
        await revoke_all_for_user(user_id)
        return None
    if as_utc(record["expires_at"]) <= now_utc():
        return None

    consumed = await db.refresh_tokens.update_one(
        {"_id": record["_id"], "revoked_at": None},
        {"$set": {"revoked_at": now_utc()}},
    )
    if consumed.modified_count == 0:
        # Lost a race with a concurrent rotation of the same token.
        return None

    new_token, expires_at = await issue_refresh_token(user_id)
    return user_id, new_token, expires_at
