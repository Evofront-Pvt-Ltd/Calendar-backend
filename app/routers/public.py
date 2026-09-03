from datetime import date
import secrets

from fastapi import APIRouter, HTTPException, Query, Request, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.widget import approved_widget_origins, request_widget_origin
from app.core.products import ensure_public_booking_token, normalize_workspace_domain
from app.core.utils import as_utc, now_utc, object_id, public_doc
from app.schemas import (
    AvailableSlotOut,
    BookingCreatePublic,
    BookingOut,
    ClientBookingCreatePublic,
    ClientBookingOut,
    ControllerVerifyOut,
    EventTypeOut,
    MemberVerifyOut,
    PublicBookingClaimOut,
    PublicLandingProductOut,
    PublicMeetingInvitationOut,
    PublicProductBookingOut,
    SlotOut,
)
from app.services.product_availability import (
    build_product_slots,
    create_client_booking,
    create_pending_client_booking,
    ensure_policy,
    client_booking_to_out,
)
from app.services.email import BookingConfirmationMessage, email_service
from app.services.scheduling import build_slots, requested_slot_is_available, timezone_or_400

router = APIRouter(prefix="/api/public", tags=["public scheduling"])


async def find_public_event(user_slug: str, event_slug: str) -> tuple[dict, dict, dict | None]:
    db = get_database()
    user = await db.users.find_one({"slug": user_slug})
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduling page not found")
    event_type = await db.event_types.find_one(
        {"owner_id": str(user["_id"]), "slug": event_slug, "active": True}
    )
    if event_type is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found")
    product = None
    product_id = event_type.get("product_id")
    if product_id:
        try:
            product = await db.products.find_one({"_id": object_id(str(product_id)), "status": "active"})
        except ValueError:
            product = None
        if product is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found")
    return user, event_type, product


async def find_public_product(booking_token: str) -> dict:
    product = await get_database().products.find_one({"public_booking_token": booking_token, "status": "active"})
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product booking link not found")
    return product


@router.get("/invitations/{token}", response_model=PublicMeetingInvitationOut)
async def public_invitation(token: str) -> PublicMeetingInvitationOut:
    db = get_database()
    invitation = await db.meeting_invitations.find_one({"secure_token": token})
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    try:
        meeting_id = object_id(invitation["meeting_id"])
        product_id = object_id(invitation["product_id"])
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found") from None

    meeting = await db.meetings.find_one({"_id": meeting_id, "product_id": invitation["product_id"]})
    product = await db.products.find_one({"_id": product_id, "status": "active"})
    if meeting is None or product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    return PublicMeetingInvitationOut(
        product_name=product["name"],
        meeting_title=meeting["title"],
        description=meeting.get("description", ""),
        start_time=meeting["start_time"],
        end_time=meeting["end_time"],
        timezone=meeting["timezone"],
        location=meeting.get("location", ""),
        meeting_url=meeting.get("meeting_url", ""),
        recipient_email=invitation["recipient_email"],
        invitation_status=invitation.get("invitation_status", "created"),
        email_delivery_status=invitation.get("email_delivery_status", "PENDING_EMAIL_INTEGRATION"),
    )


@router.get("/products/{booking_token}", response_model=PublicProductBookingOut)
async def public_product_booking(booking_token: str) -> PublicProductBookingOut:
    product = await find_public_product(booking_token)
    policy = await ensure_policy(product)
    return PublicProductBookingOut(
        product_name=product["name"],
        description=product.get("description", ""),
        timezone=policy["timezone"],
        support_start_time=policy["support_start_time"],
        support_end_time=policy["support_end_time"],
        appointment_duration_minutes=policy["appointment_duration_minutes"],
        email_enabled=settings.email_enabled,
    )


@router.get("/products", response_model=list[PublicLandingProductOut])
async def public_landing_products(
    origin: str | None = Query(default=None, description="Website origin to match against approved_domains"),
) -> list[PublicLandingProductOut]:
    db = get_database()
    query: dict[str, str] = {"status": "active"}
    if settings.organization_id:
        query["organization_id"] = settings.organization_id
    normalized_origin = normalize_workspace_domain(origin) if origin else ""
    products: list[PublicLandingProductOut] = []
    async for product in db.products.find(query).sort("name", 1):
        if normalized_origin:
            approved = approved_widget_origins(product)
            if normalized_origin not in approved:
                continue
        policy = await ensure_policy(product)
        token = await ensure_public_booking_token(product)
        products.append(
            PublicLandingProductOut(
                name=product["name"],
                description=product.get("description", ""),
                icon=product.get("icon", ""),
                color=product.get("color", "#006bff"),
                booking_token=token,
                timezone=policy["timezone"],
                support_start_time=policy["support_start_time"],
                support_end_time=policy["support_end_time"],
                appointment_duration_minutes=policy["appointment_duration_minutes"],
                booking_mode=str(product.get("booking_mode") or settings.public_booking_mode or "instant").lower(),
                widget_button_label=product.get("widget_button_label") or "Book Now",
                widget_action_label=product.get("widget_action_label") or "Schedule to connect team",
            )
        )
    return products


@router.get("/products/{booking_token}/slots", response_model=list[AvailableSlotOut])
async def public_product_slots(
    booking_token: str,
    availability_date: date | None = Query(default=None, alias="date"),
) -> list[AvailableSlotOut]:
    product = await find_public_product(booking_token)
    fake_user = {
        "_id": product.get("created_by", ""),
        "organization_id": product["organization_id"],
        "name": "Public booking",
    }
    target_date = availability_date or date.today()
    slots = await build_product_slots(product, fake_user, target_date, include_internal=False)
    return [AvailableSlotOut(**slot) for slot in slots]


@router.post("/products/{booking_token}/book", response_model=ClientBookingOut, status_code=status.HTTP_201_CREATED)
async def public_product_book(booking_token: str, payload: ClientBookingCreatePublic, request: Request) -> ClientBookingOut:
    product = await find_public_product(booking_token)
    origin = request_widget_origin(request)
    mode = str(product.get("booking_mode") or settings.public_booking_mode or "instant").lower()
    if mode in {"approval", "approval_required", "pending_approval"}:
        booking = await create_pending_client_booking(
            product, payload, source_domain=origin, widget_id=booking_token
        )
    else:
        booking = await create_client_booking(product, payload, source_domain=origin, widget_id=booking_token)
    return ClientBookingOut(**await client_booking_to_out(booking))


# Registered ahead of the /{user_slug} routes below: FastAPI matches in
# declaration order, so the slug catch-alls would otherwise swallow these.
@router.get("/controller-verify/{token}", response_model=ControllerVerifyOut)
async def public_controller_verify(token: str) -> ControllerVerifyOut:
    from app.services.product_controllers import verify_controller_token

    result = await verify_controller_token(token)
    return ControllerVerifyOut(**result)


@router.get("/member-verify/{token}", response_model=MemberVerifyOut)
async def public_member_verify(token: str) -> MemberVerifyOut:
    from app.services.product_members import verify_member_token

    return MemberVerifyOut(**await verify_member_token(token))


@router.get("/booking-claim/{token}", response_model=PublicBookingClaimOut)
async def public_booking_claim_preview(token: str) -> PublicBookingClaimOut:
    from app.services.booking_claims import public_claim_preview

    return PublicBookingClaimOut(**await public_claim_preview(token))


@router.post("/booking-claim/{token}", response_model=ClientBookingOut)
async def public_booking_claim_accept(token: str) -> ClientBookingOut:
    from app.services.booking_claims import claim_booking_by_token
    from app.services.product_availability import client_booking_to_out

    booking = await claim_booking_by_token(token)
    return ClientBookingOut(**await client_booking_to_out(booking))


@router.get("/{user_slug}")
async def public_profile(user_slug: str) -> dict:
    db = get_database()
    user = await db.users.find_one({"slug": user_slug})
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduling page not found")
    cursor = db.event_types.find({"owner_id": str(user["_id"]), "active": True}).sort("created_at", -1)
    events = []
    async for event in cursor:
        product_id = event.get("product_id")
        if product_id:
            try:
                product = await db.products.find_one({"_id": object_id(str(product_id)), "status": "active"})
            except ValueError:
                product = None
            if product is None:
                continue
        event["owner_slug"] = user["slug"]
        event["public_path"] = f"/book/{user['slug']}/{event['slug']}"
        events.append(public_doc(event))
    return {
        "user": {"name": user["name"], "slug": user["slug"], "timezone": user["timezone"]},
        "event_types": events,
    }


@router.get("/{user_slug}/{event_slug}", response_model=EventTypeOut)
async def public_event(user_slug: str, event_slug: str) -> EventTypeOut:
    user, event_type, _product = await find_public_event(user_slug, event_slug)
    event_type["owner_slug"] = user["slug"]
    event_type["public_path"] = f"/book/{user['slug']}/{event_type['slug']}"
    return EventTypeOut(**public_doc(event_type))


@router.get("/{user_slug}/{event_slug}/slots", response_model=list[SlotOut])
async def public_slots(
    user_slug: str,
    event_slug: str,
    start_date: date | None = Query(default=None),
    end_date: date | None = None,
) -> list[SlotOut]:
    user, event_type, product = await find_public_event(user_slug, event_slug)
    start_date = start_date or date.today()
    end_date = end_date or start_date
    booking_query = {"owner_id": str(user["_id"]), "status": "scheduled"}
    if event_type.get("product_id"):
        booking_query["product_id"] = str(event_type["product_id"])
    bookings = [
        item
        async for item in get_database().bookings.find(
            booking_query,
            {"start_utc": 1, "end_utc": 1},
        )
    ]
    availability = product["availability"] if product else user["availability"]
    slots = build_slots(availability, event_type, bookings, start_date, end_date)
    return [SlotOut(**slot) for slot in slots]


@router.post("/{user_slug}/{event_slug}/book", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def book_event(user_slug: str, event_slug: str, payload: BookingCreatePublic) -> BookingOut:
    timezone_or_400(payload.invitee_timezone)
    user, event_type, product = await find_public_event(user_slug, event_slug)
    requested_start = as_utc(payload.start_utc)
    availability = product["availability"] if product else user["availability"]
    lookup_date = requested_start.astimezone(timezone_or_400(availability["timezone"])).date()
    booking_query = {"owner_id": str(user["_id"]), "status": "scheduled"}
    if event_type.get("product_id"):
        booking_query["product_id"] = str(event_type["product_id"])
    bookings = [
        item
        async for item in get_database().bookings.find(
            booking_query,
            {"start_utc": 1, "end_utc": 1},
        )
    ]
    slots = build_slots(availability, event_type, bookings, lookup_date, lookup_date)
    slot = requested_slot_is_available(slots, requested_start)
    if slot is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That time is no longer available")

    timestamp = now_utc()
    booking = {
        "booking_code": secrets.token_urlsafe(10),
        "product_id": str(event_type.get("product_id", "")),
        "owner_id": str(user["_id"]),
        "event_type_id": str(event_type["_id"]),
        "event_title": event_type["title"],
        "event_slug": event_type["slug"],
        "status": "scheduled",
        "start_utc": as_utc(slot["start_utc"]),
        "end_utc": as_utc(slot["end_utc"]),
        "invitee_name": payload.invitee_name.strip(),
        "invitee_email": payload.invitee_email.lower(),
        "invitee_timezone": payload.invitee_timezone,
        "invitee_message": payload.invitee_message,
        "answers": payload.answers,
        "cancellation_reason": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await get_database().bookings.insert_one(booking)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That time was just booked") from None
    booking["_id"] = result.inserted_id
    contact_filter = {"owner_id": str(user["_id"]), "email": booking["invitee_email"]}
    if booking["product_id"]:
        contact_filter = {"product_id": booking["product_id"], "email": booking["invitee_email"]}
    await get_database().contacts.update_one(
        contact_filter,
        {
            "$set": {
                "name": booking["invitee_name"],
                "email": booking["invitee_email"],
                "product_id": booking["product_id"],
                "owner_id": str(user["_id"]),
                "source": "booking",
                "last_booking_at": booking["start_utc"],
                "updated_at": timestamp,
            },
            "$setOnInsert": {
                "company": "",
                "job_title": "",
                "notes": "",
                "created_at": timestamp,
            },
            "$inc": {"booking_count": 1},
        },
        upsert=True,
    )
    notification = {
        "organization_id": product["organization_id"] if product else user.get("organization_id", settings.organization_id),
        "product_id": booking["product_id"],
        "booking_id": str(booking["_id"]),
        "recipient_user_id": "",
        "recipient_email": booking["invitee_email"],
        "channel": "email",
        "type": "public_booking_confirmation",
        "status": "PENDING_EMAIL_INTEGRATION",
        "provider": settings.email_provider if settings.email_enabled else "disabled",
        "provider_message_id": "",
        "attempts": 0,
        "last_attempt_at": None,
        "sent_at": None,
        "delivered_at": None,
        "failed_at": None,
        "failure_reason": "",
        "idempotency_key": f"public_booking:{booking['_id']}:email:client",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        notification_result = await get_database().booking_notifications.insert_one(notification)
        notification["_id"] = notification_result.inserted_id
    except DuplicateKeyError:
        notification = {}

    if notification:
        location_detail = event_type.get("location_detail", "")
        meeting_url = location_detail if str(location_detail).startswith(("http://", "https://")) else ""
        delivery = await email_service.send_booking_confirmation(
            BookingConfirmationMessage(
                recipient_email=booking["invitee_email"],
                recipient_name=booking["invitee_name"],
                product_name=product["name"] if product else user.get("name", "Calendar Booking"),
                event_title=booking["event_title"],
                organizer_name=user.get("name", ""),
                start_time=booking["start_utc"],
                end_time=booking["end_utc"],
                timezone=booking["invitee_timezone"],
                location=location_detail,
                meeting_url=meeting_url,
                confirmation_link=f"{settings.application_base_url.rstrip('/')}/book/{user_slug}/{event_slug}",
                notes=booking.get("invitee_message", ""),
                notification_id=str(notification["_id"]),
                idempotency_key=notification["idempotency_key"],
            )
        )
        updates = {
            "status": delivery.status,
            "provider_message_id": delivery.provider_message_id,
            "failure_reason": delivery.failure_reason,
            "attempts": delivery.attempts,
            "last_attempt_at": now_utc(),
            "updated_at": now_utc(),
        }
        if delivery.status == "DELIVERED":
            updates["delivered_at"] = now_utc()
        if delivery.status in {"FAILED", "TEMPORARY_FAILURE", "BOUNCED", "DROPPED", "SPAM_REPORT"}:
            updates["failed_at"] = now_utc()
        await get_database().booking_notifications.update_one({"_id": notification["_id"]}, {"$set": updates})
    return BookingOut(**public_doc(booking))


