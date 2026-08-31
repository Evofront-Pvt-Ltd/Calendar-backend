import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import object_id

bearer_scheme = HTTPBearer(auto_error=False)

# "invited" is intentionally allowed: invited users hold no password hash and cannot reach a token anyway.
BLOCKED_USER_STATUSES = frozenset({"disabled", "suspended", "deactivated", "removed"})


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, iteration_text, salt_text, digest_text = password_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


TOKEN_TYPE_ACCESS = "access"


def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
    token_type: str = TOKEN_TYPE_ACCESS,
    claims: dict[str, Any] | None = None,
) -> str:
    expires_delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    header = {"alg": "HS256", "typ": "JWT"}
    issued_at = int(time.time())
    payload = {
        **(claims or {}),
        "sub": subject,
        "type": token_type,
        "iat": issued_at,
        "exp": int(issued_at + expires_delta.total_seconds()),
    }
    signing_input = ".".join(
        [
            _b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return f"{signing_input}.{_b64encode(signature.digest())}"


def decode_access_token(token: str, expected_type: str = TOKEN_TYPE_ACCESS) -> dict[str, Any]:
    try:
        header_text, payload_text, signature_text = token.split(".", 2)
        signing_input = f"{header_text}.{payload_text}"
        header = json.loads(_b64decode(header_text))
        if header.get("alg") != "HS256":
            raise ValueError("Unsupported algorithm")
        expected = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(_b64encode(expected.digest()), signature_text):
            raise ValueError("Invalid signature")
        payload = json.loads(_b64decode(payload_text))
        if payload.get("type") != expected_type:
            raise ValueError("Unexpected token type")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("Expired token")
        return payload
    except (ValueError, json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
        ) from None


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any] | None:
    """Resolve the caller when a usable token is present, without rejecting anonymous requests."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    payload = decode_access_token(credentials.credentials)
    try:
        user_id = object_id(str(payload["sub"]))
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject") from None

    user = await get_database().users.find_one({"_id": user_id})
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    if user.get("status") in BLOCKED_USER_STATUSES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is no longer active")
    return user

