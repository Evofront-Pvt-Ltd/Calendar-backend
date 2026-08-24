from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import get_database
from app.core.products import product_context
from app.core.security import get_current_user
from app.core.utils import now_utc, object_id, public_doc
from app.schemas import BookingOut, CancelBookingRequest

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


@router.get("", response_model=list[BookingOut])
async def list_bookings(
    status_filter: str | None = Query(default=None, alias="status"),
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> list[BookingOut]:
    context = await product_context(user, product_id, "view_product")
    query = {"product_id": str(context.product["_id"])}
    if status_filter:
        query["status"] = status_filter
    cursor = get_database().bookings.find(query).sort("start_utc", 1)
    return [BookingOut(**public_doc(item)) async for item in cursor]


@router.patch("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: str,
    payload: CancelBookingRequest,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> BookingOut:
    context = await product_context(user, product_id, "cancel_meetings", require_active=True)
    try:
        oid = object_id(booking_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found") from None
    db = get_database()
    await db.bookings.update_one(
        {"_id": oid, "product_id": str(context.product["_id"])},
        {
            "$set": {
                "status": "cancelled",
                "cancellation_reason": payload.reason,
                "updated_at": now_utc(),
            }
        },
    )
    doc = await db.bookings.find_one({"_id": oid, "product_id": str(context.product["_id"])})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return BookingOut(**public_doc(doc))
