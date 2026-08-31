"""Product controller mailboxes: add, verify (7 days), list, revoke."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.products import normalize_email, validate_organization_email
from app.core.utils import now_utc, object_id
from app.services.email import ControllerVerifyMessage, email_service

CONTROLLER_VERIFY_DAYS = 7
CONTROLLER_STATUSES = {"pending", "verified", "expired", "revoked"}


def controller_verify_link(token: str) -> str:
    return f"{settings.application_base_url.rstrip('/')}/controller-verify/{token}"


def booking_claim_link(token: str) -> str:
    return f"{settings.application_base_url.rstrip('/')}/booking-claim/{token}"


def controller_to_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "product_id": doc["product_id"],
        "email": doc["email"],
        "status": doc.get("status", "pending"),
        "added_by": doc.get("added_by", ""),
        "verified_at": doc.get("verified_at"),
        "verification_expires_at": doc.get("verification_expires_at"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


async def ensure_legacy_controller(product: dict[str, Any]) -> None:
    """Seed legacy single controller_email as verified so existing workspaces keep working."""
    email = normalize_email(str(product.get("controller_email") or ""))
    if not email:
        return
    db = get_database()
    product_id = str(product["_id"])
    existing = await db.product_controllers.find_one({"product_id": product_id, "email": email})
    if existing is not None:
        return
    timestamp = now_utc()
    try:
        await db.product_controllers.insert_one(
            {
                "product_id": product_id,
                "organization_id": product.get("organization_id", settings.organization_id),
                "email": email,
                "status": "verified",
                "verification_token": "",
                "verification_expires_at": None,
                "verified_at": timestamp,
                "added_by": product.get("created_by", ""),
                "legacy_seed": True,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
    except DuplicateKeyError:
        return


async def list_controllers(product: dict[str, Any]) -> list[dict[str, Any]]:
    await ensure_legacy_controller(product)
    await expire_pending_controllers(str(product["_id"]))
    cursor = (
        get_database()
        .product_controllers.find({"product_id": str(product["_id"]), "status": {"$ne": "revoked"}})
        .sort("created_at", 1)
    )
    return [controller_to_out(item) async for item in cursor]


async def expire_pending_controllers(product_id: str) -> None:
    now = now_utc()
    await get_database().product_controllers.update_many(
        {
            "product_id": product_id,
            "status": "pending",
            "verification_expires_at": {"$ne": None, "$lt": now},
        },
        {"$set": {"status": "expired", "updated_at": now}},
    )


async def verified_controller_emails(product: dict[str, Any]) -> list[str]:
    await ensure_legacy_controller(product)
    await expire_pending_controllers(str(product["_id"]))
    emails: list[str] = []
    seen: set[str] = set()
    async for item in get_database().product_controllers.find(
        {"product_id": str(product["_id"]), "status": "verified"}
    ):
        email = normalize_email(item.get("email", ""))
        if email and email not in seen:
            seen.add(email)
            emails.append(email)
    if not emails:
        # Never fall back to product.controller_email: add_controller stores an address
        # there before verification, so using it would leak booking data to an unverified
        # mailbox. Only the operator-configured address is an acceptable safety net.
        fallback = normalize_email(str(settings.booking_notification_email or ""))
        if fallback:
            emails.append(fallback)
    return emails


async def add_controller(product: dict[str, Any], user: dict[str, Any], email: str) -> dict[str, Any]:
    normalized = validate_organization_email(email)
    db = get_database()
    product_id = str(product["_id"])
    await expire_pending_controllers(product_id)

    existing = await db.product_controllers.find_one({"product_id": product_id, "email": normalized})
    if existing and existing.get("status") == "verified":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This controller email is already verified")
    if existing and existing.get("status") == "pending":
        return await resend_controller_verification(product, existing)

    timestamp = now_utc()
    token = secrets.token_urlsafe(32)
    doc = {
        "product_id": product_id,
        "organization_id": product.get("organization_id", settings.organization_id),
        "email": normalized,
        "status": "pending",
        "verification_token": token,
        "verification_expires_at": timestamp + timedelta(days=CONTROLLER_VERIFY_DAYS),
        "verified_at": None,
        "added_by": str(user["_id"]),
        "legacy_seed": False,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if existing and existing.get("status") in {"expired", "revoked"}:
        await db.product_controllers.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "status": "pending",
                    "verification_token": token,
                    "verification_expires_at": doc["verification_expires_at"],
                    "verified_at": None,
                    "added_by": str(user["_id"]),
                    "updated_at": timestamp,
                }
            },
        )
        doc = await db.product_controllers.find_one({"_id": existing["_id"]})
    else:
        try:
            result = await db.product_controllers.insert_one(doc)
            doc["_id"] = result.inserted_id
        except DuplicateKeyError:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This controller email already exists") from None

    await send_controller_verification_email(product, doc)
    # product.controller_email is deliberately not written here. ensure_legacy_controller
    # seeds that field as pre-verified, so writing an unverified address would let it
    # skip verification entirely. It is backfilled once verification succeeds.
    return controller_to_out(doc)


async def resend_controller_verification(product: dict[str, Any], controller: dict[str, Any]) -> dict[str, Any]:
    if controller.get("status") == "verified":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Controller email is already verified")
    if controller.get("status") == "revoked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Controller email was removed")
    timestamp = now_utc()
    token = secrets.token_urlsafe(32)
    updates = {
        "status": "pending",
        "verification_token": token,
        "verification_expires_at": timestamp + timedelta(days=CONTROLLER_VERIFY_DAYS),
        "updated_at": timestamp,
    }
    await get_database().product_controllers.update_one({"_id": controller["_id"]}, {"$set": updates})
    controller.update(updates)
    await send_controller_verification_email(product, controller)
    return controller_to_out(controller)


async def resend_by_id(product: dict[str, Any], controller_id: str) -> dict[str, Any]:
    try:
        oid = object_id(controller_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Controller not found") from None
    controller = await get_database().product_controllers.find_one(
        {"_id": oid, "product_id": str(product["_id"])}
    )
    if controller is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Controller not found")
    return await resend_controller_verification(product, controller)


async def revoke_controller(product: dict[str, Any], controller_id: str) -> None:
    try:
        oid = object_id(controller_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Controller not found") from None
    result = await get_database().product_controllers.update_one(
        {"_id": oid, "product_id": str(product["_id"]), "status": {"$ne": "revoked"}},
        {
            "$set": {
                "status": "revoked",
                "verification_token": "",
                "updated_at": now_utc(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Controller not found")


async def send_controller_verification_email(product: dict[str, Any], controller: dict[str, Any]) -> None:
    token = str(controller.get("verification_token") or "")
    if not token:
        return
    await email_service.send_controller_verification(
        ControllerVerifyMessage(
            recipient_email=controller["email"],
            recipient_name=controller["email"].split("@")[0],
            product_name=product.get("name", "Workspace"),
            verify_link=controller_verify_link(token),
            expires_days=CONTROLLER_VERIFY_DAYS,
        )
    )


async def verify_controller_token(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification link is invalid")
    db = get_database()
    controller = await db.product_controllers.find_one({"verification_token": token})
    if controller is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification link is invalid")
    if controller.get("status") == "verified":
        return {
            "status": "verified",
            "email": controller["email"],
            "product_id": controller["product_id"],
            "message": "This mailbox is already verified.",
        }
    expires = controller.get("verification_expires_at")
    if expires is not None and expires < now_utc():
        await db.product_controllers.update_one(
            {"_id": controller["_id"]},
            {"$set": {"status": "expired", "updated_at": now_utc()}},
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This verification link expired. Ask the product owner to resend it.",
        )
    timestamp = now_utc()
    await db.product_controllers.update_one(
        {"_id": controller["_id"]},
        {
            "$set": {
                "status": "verified",
                "verified_at": timestamp,
                "verification_token": "",
                "updated_at": timestamp,
            }
        },
    )
    product = await db.products.find_one({"_id": object_id(controller["product_id"])})
    if product is not None and not product.get("controller_email"):
        await db.products.update_one(
            {"_id": product["_id"]},
            {"$set": {"controller_email": controller["email"], "updated_at": timestamp}},
        )
    return {
        "status": "verified",
        "email": controller["email"],
        "product_id": controller["product_id"],
        "message": "Mailbox verified. It will receive booking-request notifications.",
    }


def public_controller_verify_out(payload: dict[str, Any]) -> dict[str, Any]:
    return payload
