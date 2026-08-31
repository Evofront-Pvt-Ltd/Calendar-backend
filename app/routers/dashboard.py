from datetime import UTC

from fastapi import APIRouter, Depends, Query

from app.core.database import get_database
from app.core.products import product_context
from app.core.security import get_current_user
from app.core.utils import now_utc

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def dashboard_stats(
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> dict:
    context = await product_context(user, product_id, "view_product")
    db = get_database()
    product_id = str(context.product["_id"])
    now = now_utc()
    total_event_types = await db.event_types.count_documents({"product_id": product_id})
    active_event_types = await db.event_types.count_documents({"product_id": product_id, "active": True})
    scheduled_bookings = await db.bookings.count_documents({"product_id": product_id, "status": "scheduled"})
    upcoming_bookings = await db.bookings.count_documents(
        {"product_id": product_id, "status": "scheduled", "start_utc": {"$gte": now.astimezone(UTC)}}
    )
    team_members = await db.product_memberships.count_documents({"product_id": product_id, "status": "active"})
    scheduled_team_meetings = await db.meetings.count_documents({"product_id": product_id, "status": "scheduled"})
    pending_invitations = await db.meeting_invitations.count_documents(
        {"product_id": product_id, "email_delivery_status": "PENDING_EMAIL_INTEGRATION"}
    )
    pending_client_bookings = await db.client_bookings.count_documents(
        {"product_id": product_id, "status": "pending_approval"}
    )
    return {
        "event_types": total_event_types,
        "active_event_types": active_event_types,
        "scheduled_bookings": scheduled_bookings,
        "upcoming_bookings": upcoming_bookings,
        "team_members": team_members,
        "scheduled_team_meetings": scheduled_team_meetings,
        "pending_invitations": pending_invitations,
        "pending_client_bookings": pending_client_bookings,
    }
