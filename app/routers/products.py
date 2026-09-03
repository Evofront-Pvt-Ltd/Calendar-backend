import secrets
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.products import (
    can_create_product,
    create_product_for_user,
    ensure_default_product,
    ensure_public_booking_token,
    permissions_for_membership,
    product_context,
    unique_user_slug,
    validate_organization_email,
    normalize_product_name,
    normalize_workspace_domains,
)
from app.core.security import get_current_user
from app.core.utils import as_utc, now_utc, object_id, public_doc
from app.schemas import (
    BookingClaimAlertOut,
    ClientBookingOut,
    MeetingCreate,
    MeetingInvitationOut,
    MeetingOut,
    MissedCallOut,
    ProductControllerCreate,
    ProductControllerOut,
    ProductCreate,
    ProductMemberCreate,
    ProductMemberOut,
    ProductMemberUpdate,
    ProductOut,
    ProductUpdate,
)
from app.services.email import InvitationEmailMessage, email_service
from app.services.google_calendar import google_calendar_service
from app.services.product_controllers import (
    add_controller,
    list_controllers,
    resend_by_id,
    revoke_controller,
)
from app.services.booking_claims import (
    claim_booking_as_user,
    list_missed_calls_for_product,
    list_open_claim_alerts_for_user,
    scan_missed_calls_for_product,
)
from app.services.product_members import (
    new_verification_payload,
    resend_member_verification,
    send_member_verification,
    verification_fields,
)
from app.services.scheduling import normalize_timezone, timezone_or_400

router = APIRouter(prefix="/api/products", tags=["products"])


def build_invitation_link(token: str) -> str:
    return f"{settings.application_base_url.rstrip('/')}/invite/{token}"


async def product_to_out(product: dict[str, Any], membership: dict[str, Any], user: dict[str, Any]) -> ProductOut:
    db = get_database()
    product_id = str(product["_id"])
    public_booking_token = await ensure_public_booking_token(product)
    member_count = await db.product_memberships.count_documents({"product_id": product_id, "status": "active"})
    doc = {
        **public_doc(product),
        "membership_role": membership.get("role", "viewer"),
        "permissions": sorted(list(permissions_for_membership(membership))),
        "can_create_product": can_create_product(user),
        "member_count": member_count,
        "public_booking_token": public_booking_token,
        "public_booking_path": f"/support/{public_booking_token}",
    }
    return ProductOut(**doc)


async def user_by_id(user_id: str) -> dict[str, Any] | None:
    try:
        oid = object_id(user_id)
    except ValueError:
        return None
    return await get_database().users.find_one({"_id": oid})


async def membership_to_out(membership: dict[str, Any]) -> ProductMemberOut:
    db = get_database()
    user = await user_by_id(membership["user_id"])
    added_by = await user_by_id(membership.get("invited_by", ""))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    return ProductMemberOut(
        id=str(membership["_id"]),
        product_id=membership["product_id"],
        user_id=str(user["_id"]),
        full_name=user.get("name", ""),
        email=user["email"],
        role=membership.get("role", "member"),
        membership_status=membership.get("status", "active"),
        invitation_status=membership.get("invitation_status", "pending_email_integration"),
        **verification_fields(membership, user),
        added_by=membership.get("invited_by", ""),
        added_by_name=added_by.get("name", "") if added_by else "",
        date_added=membership.get("created_at"),
        joined_at=membership.get("joined_at"),
        last_invitation_at=membership.get("last_invitation_at"),
        created_at=membership.get("created_at"),
        updated_at=membership.get("updated_at"),
    )


async def invitation_to_out(invitation: dict[str, Any]) -> MeetingInvitationOut:
    user = await user_by_id(invitation["recipient_user_id"])
    return MeetingInvitationOut(
        id=str(invitation["_id"]),
        meeting_id=invitation["meeting_id"],
        product_id=invitation["product_id"],
        recipient_user_id=invitation["recipient_user_id"],
        recipient_name=user.get("name", "") if user else "",
        recipient_email=invitation["recipient_email"],
        invitation_status=invitation.get("invitation_status", "created"),
        email_delivery_status=invitation.get("email_delivery_status", "PENDING_EMAIL_INTEGRATION"),
        provider_message_id=invitation.get("provider_message_id", ""),
        sent_at=invitation.get("sent_at"),
        delivered_at=invitation.get("delivered_at"),
        failed_at=invitation.get("failed_at"),
        failure_reason=invitation.get("failure_reason", ""),
        invitation_link=build_invitation_link(invitation["secure_token"]),
        created_at=invitation["created_at"],
        updated_at=invitation["updated_at"],
    )


async def meeting_to_out(meeting: dict[str, Any], include_invitations: bool = True) -> MeetingOut:
    db = get_database()
    invitations: list[MeetingInvitationOut] = []
    if include_invitations:
        cursor = db.meeting_invitations.find({"meeting_id": str(meeting["_id"])}).sort("created_at", 1)
        invitations = [await invitation_to_out(item) async for item in cursor]
    return MeetingOut(
        id=str(meeting["_id"]),
        product_id=meeting["product_id"],
        organizer_id=meeting["organizer_id"],
        title=meeting["title"],
        description=meeting.get("description", ""),
        start_time=meeting["start_time"],
        end_time=meeting["end_time"],
        timezone=meeting["timezone"],
        location=meeting.get("location", ""),
        meeting_url=meeting.get("meeting_url", ""),
        status=meeting.get("status", "scheduled"),
        invitation_count=len(invitations),
        pending_email_count=sum(1 for item in invitations if item.email_delivery_status == "PENDING_EMAIL_INTEGRATION"),
        invitations=invitations,
        created_at=meeting["created_at"],
        updated_at=meeting["updated_at"],
    )


async def load_member_user_ids(product_id: str, requested_user_ids: list[str], invite_entire_team: bool) -> list[str]:
    db = get_database()
    query: dict[str, Any] = {"product_id": product_id, "status": "active"}
    if not invite_entire_team:
        query["user_id"] = {"$in": requested_user_ids}
    memberships = [item async for item in db.product_memberships.find(query)]
    user_ids = [membership["user_id"] for membership in memberships]
    if not invite_entire_team and set(user_ids) != set(requested_user_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="One or more selected members do not belong to this product")
    return sorted(set(user_ids))


@router.get("", response_model=list[ProductOut])
async def list_products(user: dict = Depends(get_current_user)) -> list[ProductOut]:
    await ensure_default_product(user)
    db = get_database()
    memberships = [
        item
        async for item in db.product_memberships.find({"user_id": str(user["_id"]), "status": "active"}).sort("created_at", 1)
    ]
    products: list[ProductOut] = []
    for membership in memberships:
        try:
            product_oid = object_id(membership["product_id"])
        except ValueError:
            continue
        product = await db.products.find_one({"_id": product_oid, "organization_id": user.get("organization_id", settings.organization_id)})
        if product:
            products.append(await product_to_out(product, membership, user))
    return products


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate, user: dict = Depends(get_current_user)) -> ProductOut:
    if not can_create_product(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create products")
    product = await create_product_for_user(
        user=user,
        name=payload.name,
        description=payload.description,
        color=payload.color,
        icon=payload.icon,
        status_value=payload.status,
        approved_domains=payload.approved_domains,
        controller_email=payload.controller_email,
        support_email=payload.support_email,
        booking_mode=payload.booking_mode,
        widget_enabled=payload.widget_enabled,
        widget_button_label=payload.widget_button_label,
        widget_action_label=payload.widget_action_label,
        widget_position=payload.widget_position,
    )
    membership = await get_database().product_memberships.find_one(
        {"product_id": str(product["_id"]), "user_id": str(user["_id"]), "status": "active"}
    )
    return await product_to_out(product, membership, user)


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: str, user: dict = Depends(get_current_user)) -> ProductOut:
    context = await product_context(user, product_id, "view_product")
    return await product_to_out(context.product, context.membership, user)


@router.get("/{product_id}/controllers", response_model=list[ProductControllerOut])
async def get_product_controllers(product_id: str, user: dict = Depends(get_current_user)) -> list[ProductControllerOut]:
    context = await product_context(user, product_id, "view_product")
    return [ProductControllerOut(**item) for item in await list_controllers(context.product)]


@router.post("/{product_id}/controllers", response_model=ProductControllerOut, status_code=status.HTTP_201_CREATED)
async def create_product_controller(
    product_id: str,
    payload: ProductControllerCreate,
    user: dict = Depends(get_current_user),
) -> ProductControllerOut:
    context = await product_context(user, product_id, "manage_controllers", require_active=True)
    created = await add_controller(context.product, user, str(payload.email))
    return ProductControllerOut(**created)


@router.post("/{product_id}/controllers/{controller_id}/resend", response_model=ProductControllerOut)
async def resend_product_controller(
    product_id: str,
    controller_id: str,
    user: dict = Depends(get_current_user),
) -> ProductControllerOut:
    context = await product_context(user, product_id, "manage_controllers", require_active=True)
    updated = await resend_by_id(context.product, controller_id)
    return ProductControllerOut(**updated)


@router.delete("/{product_id}/controllers/{controller_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_controller(
    product_id: str,
    controller_id: str,
    user: dict = Depends(get_current_user),
) -> None:
    context = await product_context(user, product_id, "manage_controllers", require_active=True)
    await revoke_controller(context.product, controller_id)


@router.get("/{product_id}/claim-alerts", response_model=list[BookingClaimAlertOut])
async def get_claim_alerts(product_id: str, user: dict = Depends(get_current_user)) -> list[BookingClaimAlertOut]:
    await product_context(user, product_id, "view_product")
    return [BookingClaimAlertOut(**item) for item in await list_open_claim_alerts_for_user(product_id, user)]


@router.get("/{product_id}/missed-calls", response_model=list[MissedCallOut])
async def get_missed_calls(product_id: str, user: dict = Depends(get_current_user)) -> list[MissedCallOut]:
    await product_context(user, product_id, "view_product")
    return [MissedCallOut(**item) for item in await list_missed_calls_for_product(product_id)]


@router.post("/{product_id}/missed-calls/scan", response_model=list[MissedCallOut])
async def scan_product_missed_calls(product_id: str, user: dict = Depends(get_current_user)) -> list[MissedCallOut]:
    context = await product_context(user, product_id, "manage_availability", require_active=False)
    marked = await scan_missed_calls_for_product(context.product)
    return [
        MissedCallOut(
            **{
                "id": str(item["_id"]),
                "product_id": item.get("product_id", ""),
                "client_name": item.get("client_name", ""),
                "client_email": item.get("client_email", ""),
                "client_company": item.get("client_company", ""),
                "issue_title": item.get("issue_title", ""),
                "issue_category": item.get("issue_category", ""),
                "priority": item.get("priority", ""),
                "start_time": item.get("start_time_utc"),
                "end_time": item.get("end_time_utc"),
                "timezone": item.get("product_timezone", ""),
                "missed_call_at": item.get("missed_call_at"),
                "missed_call_reason": item.get("missed_call_reason", ""),
                "status": "missed",
            }
        )
        for item in marked
    ]


@router.post("/{product_id}/bookings/{booking_id}/claim", response_model=ClientBookingOut)
async def claim_product_booking(
    product_id: str,
    booking_id: str,
    user: dict = Depends(get_current_user),
) -> ClientBookingOut:
    context = await product_context(user, product_id, "view_product", require_active=True)
    booking = await claim_booking_as_user(context.product, user, booking_id)
    from app.services.product_availability import client_booking_to_out

    return ClientBookingOut(**await client_booking_to_out(booking))


@router.patch("/{product_id}", response_model=ProductOut)
async def update_product(product_id: str, payload: ProductUpdate, user: dict = Depends(get_current_user)) -> ProductOut:
    context = await product_context(user, product_id, "edit_product")
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return await product_to_out(context.product, context.membership, user)

    if "name" in updates and updates["name"] is not None:
        normalized = normalize_product_name(updates["name"])
        conflict = await get_database().products.find_one(
            {
                "organization_id": context.product["organization_id"],
                "normalized_name": normalized,
                "_id": {"$ne": context.product["_id"]},
            }
        )
        if conflict is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A product with this name already exists")
        updates["name"] = updates["name"].strip()
        updates["normalized_name"] = normalized

    if "description" in updates and updates["description"] is not None:
        updates["description"] = updates["description"].strip()
    if "icon" in updates and updates["icon"] is not None:
        updates["icon"] = updates["icon"].strip()
    if "approved_domains" in updates and updates["approved_domains"] is not None:
        updates["approved_domains"] = normalize_workspace_domains(updates["approved_domains"])
    for email_field in ("controller_email", "support_email"):
        if email_field in updates and updates[email_field] is not None:
            updates[email_field] = updates[email_field].strip().lower()
    for label_field in ("widget_button_label", "widget_action_label"):
        if label_field in updates and updates[label_field] is not None:
            updates[label_field] = updates[label_field].strip()
            if not updates[label_field]:
                updates[label_field] = "Book Now" if label_field == "widget_button_label" else "Schedule to connect team"
    updates["updated_at"] = now_utc()
    await get_database().products.update_one({"_id": context.product["_id"]}, {"$set": updates})
    product = await get_database().products.find_one({"_id": context.product["_id"]})
    return await product_to_out(product, context.membership, user)


@router.get("/{product_id}/members", response_model=list[ProductMemberOut])
async def list_members(product_id: str, user: dict = Depends(get_current_user)) -> list[ProductMemberOut]:
    context = await product_context(user, product_id, "view_members")
    cursor = get_database().product_memberships.find({"product_id": str(context.product["_id"])}).sort("created_at", 1)
    return [await membership_to_out(item) async for item in cursor]


@router.post("/{product_id}/members", response_model=ProductMemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(product_id: str, payload: ProductMemberCreate, user: dict = Depends(get_current_user)) -> ProductMemberOut:
    context = await product_context(user, product_id, "manage_members", require_active=True)
    db = get_database()
    email = validate_organization_email(str(payload.email))
    existing_user = await db.users.find_one({"email": email})
    timestamp = now_utc()

    if existing_user is None:
        user_doc = {
            "name": payload.full_name.strip(),
            "email": email,
            "email_verified": False,
            "email_verified_at": None,
            "slug": await unique_user_slug(payload.full_name),
            "timezone": user.get("timezone", "Asia/Kolkata"),
            "availability": {},
            "password_hash": "",
            "auth_provider": "invited",
            "profile_image": "",
            "organization_id": context.product["organization_id"],
            "role": "member",
            "status": "invited",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        result = await db.users.insert_one(user_doc)
        user_doc["_id"] = result.inserted_id
        existing_user = user_doc
    elif existing_user.get("organization_id", context.product["organization_id"]) != context.product["organization_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This member is not part of your organization")

    duplicate = await db.product_memberships.find_one(
        {"product_id": str(context.product["_id"]), "user_id": str(existing_user["_id"]), "status": "active"}
    )
    if duplicate is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This member already belongs to this product")

    membership = {
        "product_id": str(context.product["_id"]),
        "user_id": str(existing_user["_id"]),
        "role": payload.role,
        "status": payload.status,
        "invitation_status": "verification_sent",
        "invited_by": str(user["_id"]),
        "joined_at": None if existing_user.get("auth_provider") == "invited" else timestamp,
        "last_invitation_at": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
        **new_verification_payload(str(user["_id"])),
    }
    try:
        result = await db.product_memberships.insert_one(membership)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This member already belongs to this product") from None
    membership["_id"] = result.inserted_id
    await send_member_verification(context.product, membership, existing_user)
    return await membership_to_out(membership)


@router.post("/{product_id}/members/{membership_id}/resend-verification", response_model=ProductMemberOut)
async def resend_member_work_email_verification(
    product_id: str,
    membership_id: str,
    user: dict = Depends(get_current_user),
) -> ProductMemberOut:
    context = await product_context(user, product_id, "manage_members", require_active=True)
    membership = await resend_member_verification(context.product, membership_id)
    return await membership_to_out(membership)


@router.patch("/{product_id}/members/{membership_id}", response_model=ProductMemberOut)
async def update_member(
    product_id: str,
    membership_id: str,
    payload: ProductMemberUpdate,
    user: dict = Depends(get_current_user),
) -> ProductMemberOut:
    context = await product_context(user, product_id, "manage_members", require_active=True)
    try:
        oid = object_id(membership_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found") from None
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        membership = await get_database().product_memberships.find_one({"_id": oid, "product_id": str(context.product["_id"])})
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
        return await membership_to_out(membership)
    updates["updated_at"] = now_utc()
    await get_database().product_memberships.update_one(
        {"_id": oid, "product_id": str(context.product["_id"])},
        {"$set": updates},
    )
    membership = await get_database().product_memberships.find_one({"_id": oid, "product_id": str(context.product["_id"])})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    return await membership_to_out(membership)


@router.delete("/{product_id}/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(product_id: str, membership_id: str, user: dict = Depends(get_current_user)) -> None:
    context = await product_context(user, product_id, "manage_members", require_active=True)
    try:
        oid = object_id(membership_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found") from None
    membership = await get_database().product_memberships.find_one({"_id": oid, "product_id": str(context.product["_id"])})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    if membership["user_id"] == str(user["_id"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot remove yourself from this product")
    await get_database().product_memberships.update_one(
        {"_id": oid},
        {"$set": {"status": "inactive", "updated_at": now_utc()}},
    )


@router.get("/{product_id}/meetings", response_model=list[MeetingOut])
async def list_meetings(
    product_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    user: dict = Depends(get_current_user),
) -> list[MeetingOut]:
    context = await product_context(user, product_id, "view_invitations")
    query: dict[str, Any] = {"product_id": str(context.product["_id"])}
    if status_filter:
        query["status"] = status_filter
    cursor = get_database().meetings.find(query).sort("start_time", 1)
    return [await meeting_to_out(item) async for item in cursor]


@router.post("/{product_id}/meetings", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
async def create_meeting(product_id: str, payload: MeetingCreate, user: dict = Depends(get_current_user)) -> MeetingOut:
    context = await product_context(user, product_id, "create_meetings", require_active=True)
    if "invite_members" not in context.permissions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to invite members")
    timezone_or_400(payload.timezone)
    timezone = normalize_timezone(payload.timezone)
    start_time = as_utc(payload.start_time)
    end_time = as_utc(payload.end_time)
    if end_time <= start_time:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="end_time must be later than start_time")

    product_id = str(context.product["_id"])
    recipient_ids = await load_member_user_ids(product_id, payload.recipient_user_ids, payload.invite_entire_team)
    if not recipient_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Select at least one team member")
    recipients = []
    for recipient_id in recipient_ids:
        recipient = await user_by_id(recipient_id)
        if recipient is not None:
            recipients.append(recipient)
    if not recipients:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Select at least one valid team member")
    if settings.google_calendar_enabled:
        await google_calendar_service.ensure_member_connected_for_booking(str(user["_id"]))

    db = get_database()
    timestamp = now_utc()
    meeting = {
        "product_id": product_id,
        "organizer_id": str(user["_id"]),
        "title": payload.title.strip(),
        "description": payload.description.strip(),
        "start_time": start_time,
        "end_time": end_time,
        "timezone": timezone,
        "location": payload.location.strip(),
        "meeting_url": payload.meeting_url.strip(),
        "google_calendar_id": settings.google_calendar_id or "primary",
        "google_event_id": "",
        "google_event_url": "",
        "google_sync_status": "PENDING" if settings.google_calendar_enabled else "DISABLED",
        "google_conference_status": "",
        "google_synced_at": None,
        "google_sync_error": "",
        "status": "scheduled",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = await db.meetings.insert_one(meeting)
    meeting["_id"] = result.inserted_id

    if settings.google_calendar_enabled:
        try:
            google_result = await google_calendar_service.create_meet_event(
                organizer_user_id=str(user["_id"]),
                product_id=product_id,
                title=meeting["title"],
                description=meeting["description"],
                start_time=start_time.astimezone(UTC),
                end_time=end_time.astimezone(UTC),
                timezone=timezone,
                attendee_emails=[recipient["email"] for recipient in recipients],
                internal_record_id=str(meeting["_id"]),
                request_prefix="product-meeting",
            )
        except HTTPException as exc:
            await db.meetings.update_one(
                {"_id": meeting["_id"]},
                {
                    "$set": {
                        "google_sync_status": "FAILED",
                        "google_sync_error": "Google Calendar event creation failed",
                        "updated_at": now_utc(),
                    }
                },
            )
            raise exc
        google_updates = {
            "meeting_url": google_result.meet_url,
            "google_calendar_id": google_result.calendar_id,
            "google_event_id": google_result.event_id,
            "google_event_url": google_result.event_url,
            "google_sync_status": google_result.sync_status,
            "google_conference_status": google_result.conference_status,
            "google_synced_at": now_utc(),
            "google_sync_error": "",
            "updated_at": now_utc(),
        }
        if not google_result.meet_url:
            google_updates["google_sync_error"] = "Google Meet link is still being prepared"
            await db.meetings.update_one({"_id": meeting["_id"]}, {"$set": google_updates})
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Google Meet link is still being prepared. Please try again in a moment.",
            )
        await db.meetings.update_one({"_id": meeting["_id"]}, {"$set": google_updates})
        meeting.update(google_updates)

    for recipient in recipients:
        recipient_id = str(recipient["_id"])
        secure_token = secrets.token_urlsafe(32)
        invitation = {
            "meeting_id": str(meeting["_id"]),
            "product_id": product_id,
            "recipient_user_id": recipient_id,
            "recipient_email": recipient["email"],
            "secure_token": secure_token,
            "invitation_status": "created",
            "email_delivery_status": "PENDING_EMAIL_INTEGRATION",
            "provider_message_id": "",
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        insert_result = await db.meeting_invitations.insert_one(invitation)
        invitation["_id"] = insert_result.inserted_id
        idempotency_key = f"meeting_invitation:{invitation['_id']}:email:{recipient_id}"
        delivery = await email_service.send_meeting_invitation(
            InvitationEmailMessage(
                recipient_email=recipient["email"],
                recipient_name=recipient.get("name", ""),
                organizer_name=user.get("name", ""),
                product_name=context.product["name"],
                title=meeting["title"],
                description=meeting["description"],
                start_time=start_time.astimezone(UTC),
                end_time=end_time.astimezone(UTC),
                timezone=timezone,
                location=meeting["location"],
                meeting_url=meeting["meeting_url"],
                invitation_link=build_invitation_link(secure_token),
                invitation_id=str(invitation["_id"]),
                idempotency_key=idempotency_key,
            )
        )
        updates: dict[str, Any] = {
            "email_delivery_status": delivery.status,
            "provider_message_id": delivery.provider_message_id,
            "failure_reason": delivery.failure_reason,
            "attempts": delivery.attempts,
            "last_attempt_at": now_utc(),
            "idempotency_key": idempotency_key,
            "updated_at": now_utc(),
        }
        if delivery.status in {"QUEUED", "VALIDATED", "PROCESSED"}:
            updates["invitation_status"] = "queued"
        if delivery.status == "DELIVERED":
            updates["delivered_at"] = now_utc()
            updates["invitation_status"] = "delivered"
        if delivery.status in {"FAILED", "TEMPORARY_FAILURE", "BOUNCED", "DROPPED", "SPAM_REPORT"}:
            updates["failed_at"] = now_utc()
            updates["invitation_status"] = "email_failed"
        await db.meeting_invitations.update_one({"_id": invitation["_id"]}, {"$set": updates})

    fresh = await db.meetings.find_one({"_id": meeting["_id"]})
    return await meeting_to_out(fresh)
