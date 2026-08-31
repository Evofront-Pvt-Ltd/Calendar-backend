"""Single-use password reset tokens.

Reset tokens follow the same rules as refresh tokens: a random string of which only the
SHA-256 hash is stored, so a database leak cannot be replayed to seize an account. A token is
consumed atomically, so two clicks on the same emailed link cannot both set a password. Issuing
a new token invalidates any earlier unused one, leaving at most one live link per account.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import as_utc, now_utc

RESET_TOKEN_BYTES = 32

# Repeat requests inside this window reuse the live link instead of mailing another, so the
# endpoint cannot be used to flood someone's inbox.
RESET_RESEND_COOLDOWN_SECONDS = 60


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_token_lifetime() -> timedelta:
    return timedelta(minutes=settings.password_reset_expire_minutes)


def reset_password_link(token: str) -> str:
    return f"{settings.application_base_url.rstrip('/')}/reset-password/{token}"


async def revoke_unused_for_user(user_id: str) -> int:
    result = await get_database().password_resets.update_many(
        {"user_id": str(user_id), "used_at": None},
        {"$set": {"used_at": now_utc(), "revoked": True}},
    )
    return result.modified_count


async def live_reset_within_cooldown(user_id: str) -> bool:
    """True when a usable link was mailed recently, so another send would be redundant."""
    record = await get_database().password_resets.find_one(
        {"user_id": str(user_id), "used_at": None},
        sort=[("created_at", -1)],
    )
    if record is None:
        return False
    if as_utc(record["expires_at"]) <= now_utc():
        return False
    age = (now_utc() - as_utc(record["created_at"])).total_seconds()
    return age < RESET_RESEND_COOLDOWN_SECONDS


async def issue_password_reset(user: dict[str, Any]) -> tuple[str, datetime]:
    user_id = str(user["_id"])
    await revoke_unused_for_user(user_id)
    token = secrets.token_urlsafe(RESET_TOKEN_BYTES)
    issued_at = now_utc()
    expires_at = issued_at + reset_token_lifetime()
    await get_database().password_resets.insert_one(
        {
            "user_id": user_id,
            # Stored for audit only; the token is what authorises the reset.
            "email": str(user.get("email", "")),
            "token_hash": hash_reset_token(token),
            "created_at": issued_at,
            "expires_at": expires_at,
            "used_at": None,
            "revoked": False,
        }
    )
    return token, expires_at


async def find_live_reset(token: str) -> dict[str, Any] | None:
    """Look up a usable token without consuming it, for rendering the reset form."""
    token = (token or "").strip()
    if not token:
        return None
    record = await get_database().password_resets.find_one({"token_hash": hash_reset_token(token)})
    if record is None or record.get("used_at") is not None:
        return None
    if as_utc(record["expires_at"]) <= now_utc():
        return None
    return record


async def consume_password_reset(token: str) -> str | None:
    """Spend a reset token and return its user_id, or None when it is not usable."""
    record = await find_live_reset(token)
    if record is None:
        return None
    consumed = await get_database().password_resets.update_one(
        {"_id": record["_id"], "used_at": None},
        {"$set": {"used_at": now_utc()}},
    )
    if consumed.modified_count == 0:
        # Lost a race with a concurrent use of the same link.
        return None
    return str(record.get("user_id", ""))
