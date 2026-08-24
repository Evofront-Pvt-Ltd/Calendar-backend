import re
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import now_utc, object_id, unique_slug
from app.services.scheduling import default_availability


PRODUCT_PERMISSIONS = {
    "product_owner": {
        "view_product",
        "edit_product",
        "deactivate_product",
        "view_members",
        "manage_members",
        "create_meetings",
        "invite_members",
        "reschedule_meetings",
        "cancel_meetings",
        "view_invitations",
        "manage_availability",
        "manage_event_types",
        "manage_contacts",
    },
    "calendar_controller": {
        "view_product",
        "edit_product",
        "view_members",
        "manage_members",
        "create_meetings",
        "invite_members",
        "reschedule_meetings",
        "cancel_meetings",
        "view_invitations",
        "manage_availability",
        "manage_event_types",
        "manage_contacts",
    },
    "member": {
        "view_product",
        "view_members",
        "view_invitations",
    },
    "viewer": {
        "view_product",
        "view_members",
    },
}

GLOBAL_PRODUCT_CREATORS = {"organization_admin", "calendar_controller"}


@dataclass(frozen=True)
class ProductContext:
    product: dict[str, Any]
    membership: dict[str, Any]
    permissions: set[str]


def normalize_product_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_organization_email(email: str) -> str:
    normalized = normalize_email(email)
    domain = settings.organization_email_domain.strip().lower().lstrip("@")
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Organization email domain is not configured",
        )
    if not normalized.endswith(f"@{domain}"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Team member email must belong to @{domain}",
        )
    return normalized


def global_user_role(user: dict[str, Any]) -> str:
    return str(user.get("role") or "calendar_controller")


def can_create_product(user: dict[str, Any]) -> bool:
    return global_user_role(user) in GLOBAL_PRODUCT_CREATORS


def permissions_for_membership(membership: dict[str, Any]) -> set[str]:
    return set(PRODUCT_PERMISSIONS.get(str(membership.get("role") or "viewer"), set()))


async def ensure_user_metadata(user: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if not user.get("organization_id"):
        updates["organization_id"] = settings.organization_id
    if not user.get("role"):
        updates["role"] = "calendar_controller"
    if not user.get("status"):
        updates["status"] = "active"
    if updates:
        updates["updated_at"] = now_utc()
        await get_database().users.update_one({"_id": user["_id"]}, {"$set": updates})
        user.update(updates)
    return user


async def create_product_for_user(
    user: dict[str, Any],
    name: str,
    description: str = "",
    color: str = "#006bff",
    icon: str = "",
    status_value: str = "active",
) -> dict[str, Any]:
    await ensure_user_metadata(user)
    db = get_database()
    normalized_name = normalize_product_name(name)
    if await db.products.find_one(
        {"organization_id": user["organization_id"], "normalized_name": normalized_name}
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A product with this name already exists")

    timestamp = now_utc()
    availability = user.get("availability") or default_availability(user.get("timezone", "Asia/Kolkata"))
    product = {
        "organization_id": user["organization_id"],
        "name": name.strip(),
        "normalized_name": normalized_name,
        "description": description.strip(),
        "color": color,
        "icon": icon.strip(),
        "status": status_value,
        "availability": availability,
        "public_booking_token": await unique_public_booking_token(),
        "created_by": str(user["_id"]),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await db.products.insert_one(product)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A product with this name already exists")

    product["_id"] = result.inserted_id
    product_id = str(result.inserted_id)
    membership = {
        "product_id": product_id,
        "user_id": str(user["_id"]),
        "role": "product_owner",
        "status": "active",
        "invitation_status": "accepted",
        "invited_by": str(user["_id"]),
        "joined_at": timestamp,
        "last_invitation_at": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    await db.product_memberships.update_one(
        {"product_id": product_id, "user_id": str(user["_id"]), "status": "active"},
        {"$setOnInsert": membership},
        upsert=True,
    )
    return product


async def unique_public_booking_token() -> str:
    db = get_database()
    while True:
        token = secrets.token_urlsafe(18)
        if not await db.products.find_one({"public_booking_token": token}):
            return token


async def ensure_public_booking_token(product: dict[str, Any]) -> str:
    token = str(product.get("public_booking_token") or "")
    if token:
        return token
    token = await unique_public_booking_token()
    await get_database().products.update_one(
        {"_id": product["_id"]},
        {"$set": {"public_booking_token": token, "updated_at": now_utc()}},
    )
    product["public_booking_token"] = token
    return token


async def ensure_default_product(user: dict[str, Any]) -> dict[str, Any]:
    await ensure_user_metadata(user)
    db = get_database()
    user_id = str(user["_id"])
    membership = await db.product_memberships.find_one({"user_id": user_id, "status": "active"})
    if membership is not None:
        try:
            product_oid = object_id(membership["product_id"])
        except ValueError:
            product_oid = None
        product = await db.products.find_one({"_id": product_oid}) if product_oid is not None else None
        if product is not None:
            await attach_legacy_records(user, str(product["_id"]))
            return product

    base_name = f"{user.get('name', 'My')} Workspace"
    name = base_name
    index = 2
    while True:
        try:
            product = await create_product_for_user(user, name=name)
            await attach_legacy_records(user, str(product["_id"]))
            return product
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
            name = f"{base_name} {index}"
            index += 1


async def attach_legacy_records(user: dict[str, Any], product_id: str) -> None:
    db = get_database()
    user_id = str(user["_id"])
    await db.event_types.update_many(
        {"owner_id": user_id, "product_id": {"$exists": False}},
        {"$set": {"product_id": product_id}},
    )
    await db.bookings.update_many(
        {"owner_id": user_id, "product_id": {"$exists": False}},
        {"$set": {"product_id": product_id}},
    )
    await db.contacts.update_many(
        {"owner_id": user_id, "product_id": {"$exists": False}},
        {"$set": {"product_id": product_id}},
    )


async def product_context(
    user: dict[str, Any],
    product_id: str | None,
    permission: str = "view_product",
    require_active: bool = False,
) -> ProductContext:
    await ensure_user_metadata(user)
    db = get_database()
    if not product_id:
        product = await ensure_default_product(user)
        product_id = str(product["_id"])
    else:
        try:
            product_oid = object_id(product_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found") from None
        product = await db.products.find_one({"_id": product_oid, "organization_id": user["organization_id"]})
        if product is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    membership = await db.product_memberships.find_one(
        {"product_id": product_id, "user_id": str(user["_id"]), "status": "active"}
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot access this product")

    if require_active and product.get("status") != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This product is inactive")

    permissions = permissions_for_membership(membership)
    if permission and permission not in permissions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission for this product")

    return ProductContext(product=product, membership=membership, permissions=permissions)


async def unique_user_slug(name: str) -> str:
    return await unique_slug(get_database().users, name)
