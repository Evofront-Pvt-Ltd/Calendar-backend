"""Team member work-email verification.

Verification is independent of dashboard login: a verified member receives
booking alerts by email whether or not they ever sign in. Membership rows
carry the verification state so rotation eligibility never depends on a
password being set.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import as_utc, now_utc, object_id
from app.services.email import MemberVerifyMessage, email_service

MEMBER_VERIFY_DAYS = 7
MEMBER_VERIFICATION_STATUSES = {"pending", "verified", "expired"}


def member_verify_link(token: str) -> str:
    return f"{settings.application_base_url.rstrip('/')}/member-verify/{token}"


def has_login_identity(user: dict[str, Any] | None) -> bool:
    """True when the member can sign in to the dashboard."""
    if not user:
        return False
    if user.get("password_hash"):
        return True
    return user.get("auth_provider") in {"password", "google", "password_google"}


def verification_state(membership: dict[str, Any], user: dict[str, Any] | None = None) -> str:
    """Verification status for a membership, with a fallback for legacy rows.

    Memberships created before member verification existed have no stored
    state. Those already backed by an active login count as verified so
    existing workspaces keep their rotation.
    """
    stored = str(membership.get("member_verification_status") or "").strip().lower()
    if stored in MEMBER_VERIFICATION_STATUSES:
        if stored == "pending":
            expires = membership.get("member_verification_expires_at")
            if isinstance(expires, datetime) and as_utc(expires) < now_utc():
                return "expired"
        return stored
    if user is not None and user.get("status") == "active":
        return "verified"
    if membership.get("invitation_status") == "accepted":
        return "verified"
    return "pending"


def is_verified(membership: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    return verification_state(membership, user) == "verified"


def verification_reason(state: str) -> str:
    if state == "pending":
        return "Work email not verified yet"
    if state == "expired":
        return "Verification link expired"
    return ""


def verification_fields(membership: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    state = verification_state(membership, user)
    return {
        "verification_status": state,
        "verified_at": membership.get("member_verified_at"),
        "verification_expires_at": membership.get("member_verification_expires_at"),
        "has_login": has_login_identity(user),
    }


def new_verification_payload(invited_by: str) -> dict[str, Any]:
    timestamp = now_utc()
    return {
        "member_verification_status": "pending",
        "member_verification_token": secrets.token_urlsafe(32),
        "member_verification_expires_at": timestamp + timedelta(days=MEMBER_VERIFY_DAYS),
        "member_verified_at": None,
        "member_verification_sent_at": timestamp,
        "member_verification_requested_by": invited_by,
    }


async def send_member_verification(
    product: dict[str, Any],
    membership: dict[str, Any],
    user: dict[str, Any],
) -> None:
    token = str(membership.get("member_verification_token") or "")
    if not token:
        return
    await email_service.send_member_verification(
        MemberVerifyMessage(
            recipient_email=user["email"],
            recipient_name=user.get("name", "") or user["email"].split("@")[0],
            product_name=product.get("name", "Workspace"),
            role=str(membership.get("role", "member")).replace("_", " "),
            verify_link=member_verify_link(token),
            expires_days=MEMBER_VERIFY_DAYS,
        )
    )


async def resend_member_verification(product: dict[str, Any], membership_id: str) -> dict[str, Any]:
    db = get_database()
    product_id = str(product["_id"])
    try:
        oid = object_id(membership_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found") from None
    membership = await db.product_memberships.find_one({"_id": oid, "product_id": product_id})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    user = await db.users.find_one({"_id": object_id(membership["user_id"])})
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    if verification_state(membership, user) == "verified":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This work email is already verified")

    updates = new_verification_payload(membership.get("invited_by", ""))
    updates["invitation_status"] = "verification_sent"
    updates["last_invitation_at"] = updates["member_verification_sent_at"]
    updates["updated_at"] = now_utc()
    await db.product_memberships.update_one({"_id": membership["_id"]}, {"$set": updates})
    membership.update(updates)
    await send_member_verification(product, membership, user)
    return membership


async def verify_member_token(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification link is invalid")
    db = get_database()
    membership = await db.product_memberships.find_one({"member_verification_token": token})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification link is invalid")

    user = await db.users.find_one({"_id": object_id(membership["user_id"])})
    product = await db.products.find_one({"_id": object_id(membership["product_id"])})
    email = user.get("email", "") if user else ""
    product_name = product.get("name", "") if product else ""

    if membership.get("member_verification_status") == "verified":
        return {
            "status": "verified",
            "email": email,
            "product_name": product_name,
            "has_login": has_login_identity(user),
            "message": "This work email is already verified.",
        }

    expires = membership.get("member_verification_expires_at")
    if isinstance(expires, datetime) and as_utc(expires) < now_utc():
        await db.product_memberships.update_one(
            {"_id": membership["_id"]},
            {"$set": {"member_verification_status": "expired", "updated_at": now_utc()}},
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This verification link expired. Ask the product owner to resend it.",
        )

    timestamp = now_utc()
    await db.product_memberships.update_one(
        {"_id": membership["_id"]},
        {
            "$set": {
                "member_verification_status": "verified",
                "member_verified_at": timestamp,
                "member_verification_token": "",
                "invitation_status": "verified",
                "joined_at": membership.get("joined_at") or timestamp,
                "updated_at": timestamp,
            }
        },
    )
    return {
        "status": "verified",
        "email": email,
        "product_name": product_name,
        "has_login": has_login_identity(user),
        "message": "Work email verified. You will receive booking alerts for this workspace.",
    }
