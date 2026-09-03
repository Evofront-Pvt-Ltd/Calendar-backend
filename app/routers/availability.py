from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import get_database
from app.core.products import product_context
from app.core.security import get_current_user
from app.core.utils import now_utc, object_id, public_doc
from app.schemas import (
    AvailabilityAuditLogOut,
    AvailabilityExceptionCreate,
    AvailabilityExceptionOut,
    AvailabilityIn,
    BookingAssignmentUpdate,
    BookingDecisionRequest,
    CancelBookingRequest,
    ClientBookingOut,
    GenerateCoverageRequest,
    MemberAvailabilityOut,
    MemberAvailabilityUpsert,
    ProductAvailabilityPolicyIn,
    ProductAvailabilityPolicyOut,
    TeamAvailabilityOut,
)
from app.services.product_availability import (
    audit_availability,
    release_client_booking_to_team,
    assign_client_booking,
    cancel_client_booking,
    client_booking_to_out,
    ensure_policy,
    generate_equal_coverage,
    reject_client_booking,
    team_availability_context,
)
from app.services.scheduling import normalize_timezone, timezone_or_400

router = APIRouter(prefix="/api/availability", tags=["availability"])


@router.get("", response_model=AvailabilityIn)
async def get_availability(
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> AvailabilityIn:
    context = await product_context(user, product_id, "view_product")
    return AvailabilityIn(**context.product["availability"])


@router.put("", response_model=AvailabilityIn)
async def update_availability(
    payload: AvailabilityIn,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> AvailabilityIn:
    context = await product_context(user, product_id, "manage_availability", require_active=True)
    timezone_or_400(payload.timezone)
    availability = payload.model_dump()
    availability["timezone"] = normalize_timezone(payload.timezone)
    await get_database().products.update_one(
        {"_id": context.product["_id"]},
        {"$set": {"availability": availability, "updated_at": now_utc()}},
    )
    return AvailabilityIn(**public_doc(availability))


@router.get("/team", response_model=TeamAvailabilityOut)
async def get_team_availability(
    product_id: str = Query(),
    availability_date: date | None = Query(default=None, alias="date"),
    user: dict = Depends(get_current_user),
) -> TeamAvailabilityOut:
    context = await product_context(user, product_id, "view_product")
    target_date = availability_date or date.today()
    return TeamAvailabilityOut(**await team_availability_context(context.product, user, target_date))


@router.put("/policy", response_model=ProductAvailabilityPolicyOut)
async def update_policy(
    payload: ProductAvailabilityPolicyIn,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> ProductAvailabilityPolicyOut:
    context = await product_context(user, product_id, "manage_availability", require_active=True)
    timezone_or_400(payload.timezone)
    db = get_database()
    previous = await ensure_policy(context.product, user)
    timestamp = now_utc()
    policy = {
        **payload.model_dump(),
        "timezone": normalize_timezone(payload.timezone),
        "organization_id": context.product["organization_id"],
        "product_id": str(context.product["_id"]),
        "updated_by": str(user["_id"]),
        "updated_at": timestamp,
    }
    await db.availability_policies.update_one(
        {"product_id": str(context.product["_id"])},
        {
            "$set": policy,
            "$setOnInsert": {
                "created_by": str(user["_id"]),
                "created_at": timestamp,
            },
        },
        upsert=True,
    )
    saved = await db.availability_policies.find_one({"product_id": str(context.product["_id"])})
    await audit_availability(context.product, user, "update_policy", previous, saved)
    return ProductAvailabilityPolicyOut(**public_doc(saved))


@router.post("/generate", response_model=list[MemberAvailabilityOut])
async def generate_coverage(
    payload: GenerateCoverageRequest,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> list[MemberAvailabilityOut]:
    context = await product_context(user, product_id, "manage_availability", require_active=True)
    records = await generate_equal_coverage(
        context.product,
        user,
        payload.date,
        preserve_manual_overrides=payload.preserve_manual_overrides,
        force_regenerate=payload.force_regenerate,
    )
    return [
        MemberAvailabilityOut(**item)
        for item in (await team_availability_context(context.product, user, payload.date))["coverage"]
        if item["id"] in {str(record["_id"]) for record in records} or not records
    ]


async def ensure_member_belongs_to_product(product_id: str, member_id: str) -> dict:
    membership = await get_database().product_memberships.find_one(
        {"product_id": product_id, "user_id": member_id, "status": "active"}
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    return membership


@router.put("/members/{member_id}", response_model=MemberAvailabilityOut)
async def upsert_member_availability(
    member_id: str,
    payload: MemberAvailabilityUpsert,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> MemberAvailabilityOut:
    context = await product_context(user, product_id, "view_product", require_active=True)
    if member_id != payload.member_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Member mismatch")
    if "manage_availability" not in context.permissions and member_id != str(user["_id"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own availability")
    await ensure_member_belongs_to_product(str(context.product["_id"]), member_id)
    timezone_or_400(payload.timezone)
    db = get_database()
    timestamp = now_utc()
    date_key = payload.date.isoformat()
    previous = await db.member_availabilities.find_one(
        {
            "product_id": str(context.product["_id"]),
            "member_id": member_id,
            "date": date_key,
            "source": "MANUAL",
        }
    )
    record = {
        "organization_id": context.product["organization_id"],
        "product_id": str(context.product["_id"]),
        "member_id": member_id,
        "day_of_week": payload.date.weekday(),
        "date": date_key,
        "start_time": payload.start_time,
        "end_time": payload.end_time,
        "timezone": normalize_timezone(payload.timezone),
        "recurrence_rule": "",
        "source": "MANUAL",
        "status": payload.status,
        "effective_from": date_key,
        "effective_until": date_key,
        "updated_by": str(user["_id"]),
        "updated_at": timestamp,
    }
    await db.member_availabilities.update_one(
        {
            "product_id": str(context.product["_id"]),
            "member_id": member_id,
            "date": date_key,
            "source": "MANUAL",
        },
        {
            "$set": record,
            "$setOnInsert": {
                "created_by": str(user["_id"]),
                "created_at": timestamp,
            },
        },
        upsert=True,
    )
    saved = await db.member_availabilities.find_one(
        {
            "product_id": str(context.product["_id"]),
            "member_id": member_id,
            "date": date_key,
            "source": "MANUAL",
        }
    )
    await audit_availability(
        context.product,
        user,
        "upsert_member_availability",
        previous,
        saved,
        member_id=member_id,
        reason=payload.change_reason,
    )
    decorated = [
        item
        for item in (await team_availability_context(context.product, user, payload.date))["coverage"]
        if item["id"] == str(saved["_id"])
    ]
    if decorated:
        return MemberAvailabilityOut(**decorated[0])
    return MemberAvailabilityOut(**public_doc(saved), member_name=user.get("name", ""), member_role=context.membership.get("role", ""))


@router.post("/exceptions", response_model=AvailabilityExceptionOut, status_code=status.HTTP_201_CREATED)
async def create_exception(
    payload: AvailabilityExceptionCreate,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> AvailabilityExceptionOut:
    context = await product_context(user, product_id, "view_product", require_active=True)
    target_member_id = payload.member_id or str(user["_id"])
    if "manage_availability" not in context.permissions and target_member_id != str(user["_id"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own exceptions")
    if target_member_id:
        await ensure_member_belongs_to_product(str(context.product["_id"]), target_member_id)
    timestamp = now_utc()
    doc = {
        **payload.model_dump(),
        "exception_date": payload.exception_date.isoformat(),
        "member_id": target_member_id,
        "organization_id": context.product["organization_id"],
        "product_id": str(context.product["_id"]),
        "created_by": str(user["_id"]),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = await get_database().availability_exceptions.insert_one(doc)
    doc["_id"] = result.inserted_id
    await audit_availability(
        context.product,
        user,
        "create_exception",
        {},
        doc,
        member_id=target_member_id,
        reason=payload.reason,
    )
    return AvailabilityExceptionOut(**public_doc(doc))


@router.get("/bookings/{booking_id}")
async def get_client_booking(
    booking_id: str,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> dict:
    context = await product_context(user, product_id, "view_product")
    try:
        oid = object_id(booking_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found") from None
    booking = await get_database().client_bookings.find_one({"_id": oid, "product_id": str(context.product["_id"])})
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return await client_booking_to_out(booking)


@router.patch("/bookings/{booking_id}/cancel", response_model=ClientBookingOut)
async def cancel_product_client_booking(
    booking_id: str,
    payload: CancelBookingRequest,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> ClientBookingOut:
    context = await product_context(user, product_id, "view_product", require_active=True)
    booking = await cancel_client_booking(
        context.product,
        user,
        booking_id,
        reason=payload.reason,
        permissions=context.permissions,
    )
    return ClientBookingOut(**await client_booking_to_out(booking))


@router.patch("/bookings/{booking_id}/assignment", response_model=ClientBookingOut)
async def assign_product_client_booking(
    booking_id: str,
    payload: BookingAssignmentUpdate,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> ClientBookingOut:
    context = await product_context(user, product_id, "manage_availability", require_active=True)
    booking = await assign_client_booking(
        context.product,
        user,
        booking_id,
        member_id=payload.member_id,
        reason=payload.reason,
    )
    return ClientBookingOut(**await client_booking_to_out(booking))


@router.post("/bookings/{booking_id}/approve", response_model=ClientBookingOut)
async def approve_product_client_booking(
    booking_id: str,
    payload: BookingDecisionRequest,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> ClientBookingOut:
    """Release a pending request to the team (awaiting_acceptance + claim alerts). Scheduling happens on claim."""
    context = await product_context(user, product_id, "manage_availability", require_active=True)
    booking = await release_client_booking_to_team(context.product, user, booking_id, reason=payload.reason)
    return ClientBookingOut(**await client_booking_to_out(booking))


@router.post("/bookings/{booking_id}/reject", response_model=ClientBookingOut)
async def reject_product_client_booking(
    booking_id: str,
    payload: BookingDecisionRequest,
    product_id: str = Query(),
    user: dict = Depends(get_current_user),
) -> ClientBookingOut:
    context = await product_context(user, product_id, "manage_availability", require_active=True)
    booking = await reject_client_booking(context.product, user, booking_id, reason=payload.reason)
    return ClientBookingOut(**await client_booking_to_out(booking))


@router.get("/audit", response_model=list[AvailabilityAuditLogOut])
async def audit_history(
    product_id: str = Query(),
    member_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> list[AvailabilityAuditLogOut]:
    context = await product_context(user, product_id, "manage_availability")
    query = {"product_id": str(context.product["_id"])}
    if member_id:
        query["member_id"] = member_id
    cursor = get_database().availability_audit_logs.find(query).sort("created_at", -1).limit(100)
    return [AvailabilityAuditLogOut(**public_doc(item)) async for item in cursor]
