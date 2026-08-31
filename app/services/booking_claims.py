"""Shift-based booking claim alerts (dashboard + email), first-wins."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import as_utc, now_utc, object_id
from app.services.email import BookingNotificationMessage, email_service
from app.services.product_controllers import booking_claim_link, verified_controller_emails
from app.services.product_members import has_login_identity, is_verified
from app.services.product_availability import (
    SUPPORT_ROLES,
    approve_client_booking,
    assign_client_booking,
    confirmation_link,
    coverage_records_for_date,
    eligible_product_members,
    ensure_policy,
    local_window_to_utc,
    ranges_overlap,
    update_notification_delivery,
)

CONTROLLER_ROLES = {"product_owner", "calendar_controller"}

# A claim link is only meaningful until the meeting it offers begins, and never
# longer than this window even for bookings scheduled far ahead.
CLAIM_TOKEN_MAX_DAYS = 14


async def members_on_shift_for_booking(product: dict[str, Any], booking: dict[str, Any]) -> list[dict[str, Any]]:
    """Return eligible users whose coverage window overlaps the booking slot."""
    policy = await ensure_policy(product)
    timezone = booking.get("product_timezone") or policy["timezone"]
    start = as_utc(booking["start_time_utc"])
    end = as_utc(booking["end_time_utc"])
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    local_start = start.astimezone(tz)
    target_dates = {local_start.date()}
    if end.astimezone(tz).date() != local_start.date():
        target_dates.add(end.astimezone(tz).date())
        target_dates.add(local_start.date() - timedelta(days=1))

    # Use a system-like context user for coverage generation (product creator).
    owner_id = product.get("created_by") or ""
    context_user: dict[str, Any] = {"_id": owner_id}
    try:
        if owner_id:
            found = await get_database().users.find_one({"_id": object_id(owner_id)})
            if found:
                context_user = found
    except ValueError:
        pass

    overlapping_ids: set[str] = set()
    for target_date in sorted(target_dates):
        try:
            records = await coverage_records_for_date(product, context_user, target_date)
        except Exception:
            records = []
        for record in records:
            if record.get("status") != "available":
                continue
            window_start, window_end = local_window_to_utc(
                target_date,
                record["start_time"],
                record["end_time"],
                record.get("timezone") or timezone,
            )
            if ranges_overlap(start, end, window_start, window_end):
                overlapping_ids.add(str(record["member_id"]))

    if not overlapping_ids:
        return []

    eligible = await eligible_product_members(str(product["_id"]))
    return [item["user"] for item in eligible if str(item["user"]["_id"]) in overlapping_ids]


async def fallback_controller_users(product: dict[str, Any]) -> list[dict[str, Any]]:
    """When nobody is on shift, alert calendar controllers / product owners."""
    db = get_database()
    product_id = str(product["_id"])
    users: list[dict[str, Any]] = []
    seen: set[str] = set()
    async for membership in db.product_memberships.find({"product_id": product_id, "status": "active"}):
        if membership.get("role") not in CONTROLLER_ROLES:
            continue
        try:
            user = await db.users.find_one({"_id": object_id(membership["user_id"])})
        except ValueError:
            user = None
        if user is None or not is_verified(membership, user):
            continue
        uid = str(user["_id"])
        if uid in seen:
            continue
        seen.add(uid)
        users.append(user)

    # Also include users matching verified controller mailbox emails.
    for email in await verified_controller_emails(product):
        user = await db.users.find_one({"email": email})
        if user is None:
            continue
        uid = str(user["_id"])
        if uid in seen:
            continue
        # Must be a product member to claim.
        membership = await db.product_memberships.find_one(
            {"product_id": product_id, "user_id": uid, "status": "active"}
        )
        if membership is None or membership.get("role") not in SUPPORT_ROLES | CONTROLLER_ROLES:
            continue
        if not is_verified(membership, user):
            continue
        seen.add(uid)
        users.append(user)
    return users


async def recipients_for_claim_alerts(product: dict[str, Any], booking: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    on_shift = await members_on_shift_for_booking(product, booking)
    if on_shift:
        return on_shift, "shift"
    return await fallback_controller_users(product), "controllers_fallback"


async def create_controller_mailbox_notifications(
    product: dict[str, Any],
    booking: dict[str, Any],
) -> list[dict[str, Any]]:
    """Email verified controller mailboxes only (never unverified)."""
    emails = await verified_controller_emails(product)
    if not emails:
        return []
    db = get_database()
    timestamp = now_utc()
    notifications: list[dict[str, Any]] = []
    for email in emails:
        notification = {
            "organization_id": product["organization_id"],
            "product_id": str(product["_id"]),
            "booking_id": str(booking["_id"]),
            "recipient_user_id": "",
            "recipient_email": email,
            "channel": "email",
            "type": "controller_booking_request_created",
            "status": "PENDING_EMAIL_INTEGRATION",
            "provider": settings.email_provider if settings.email_enabled else "disabled",
            "provider_message_id": "",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": "",
            "idempotency_key": f"client_booking:{booking['_id']}:email:controller:{email}",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = await db.booking_notifications.insert_one(notification)
            notification["_id"] = result.inserted_id
        except DuplicateKeyError:
            continue
        delivery = await email_service.send_booking_notification(
            BookingNotificationMessage(
                recipient_email=email,
                recipient_name=product.get("name", "Workspace controller"),
                product_name=product["name"],
                client_name=booking["client_name"],
                client_company=booking.get("client_company", ""),
                issue_category=booking["issue_category"],
                issue_title=booking["issue_title"],
                issue_description=booking.get("issue_description", ""),
                priority=booking["priority"],
                start_time=booking["start_time_utc"],
                end_time=booking["end_time_utc"],
                timezone=booking["product_timezone"],
                duration_minutes=int((booking["end_time_utc"] - booking["start_time_utc"]).total_seconds() // 60),
                booking_link=confirmation_link(booking["secure_token_reference"]),
                client_phone=booking.get("client_phone", ""),
                product_reference_number=booking.get("product_reference_number", ""),
                meeting_url="",
                booking_status=booking.get("status", "pending_approval"),
                source_domain=str(booking.get("source_domain") or ""),
                reply_to_email=booking["client_email"] if settings.booking_reply_to_enabled else "",
                notification_id=str(notification["_id"]),
                idempotency_key=notification["idempotency_key"],
            )
        )
        await update_notification_delivery(notification, delivery)
        notifications.append(notification)
    return notifications


async def create_shift_claim_alerts(product: dict[str, Any], booking: dict[str, Any]) -> list[dict[str, Any]]:
    recipients, audience = await recipients_for_claim_alerts(product, booking)
    if not recipients:
        return []
    db = get_database()
    timestamp = now_utc()
    claim_expires_at = min(as_utc(booking["start_time_utc"]), timestamp + timedelta(days=CLAIM_TOKEN_MAX_DAYS))
    alerts: list[dict[str, Any]] = []
    for user in recipients:
        claim_token = secrets.token_urlsafe(24)
        alert = {
            "organization_id": product["organization_id"],
            "product_id": str(product["_id"]),
            "booking_id": str(booking["_id"]),
            "recipient_user_id": str(user["_id"]),
            "recipient_email": user.get("email", ""),
            "status": "open",
            "audience": audience,
            "claim_token": claim_token,
            "claim_expires_at": claim_expires_at,
            "claimed_at": None,
            "claimed_by": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = await db.booking_claim_alerts.insert_one(alert)
            alert["_id"] = result.inserted_id
        except DuplicateKeyError:
            continue
        alerts.append(alert)

        # Members without a dashboard login are alerted by email only.
        if settings.in_app_notifications_enabled and settings.notifications_enabled and has_login_identity(user):
            in_app = {
                "organization_id": product["organization_id"],
                "product_id": str(product["_id"]),
                "booking_id": str(booking["_id"]),
                "recipient_user_id": str(user["_id"]),
                "recipient_email": user.get("email", ""),
                "channel": "in_app",
                "type": "booking_claim_alert",
                "status": "UNREAD",
                "provider": "in_app",
                "provider_message_id": "",
                "attempts": 0,
                "last_attempt_at": None,
                "sent_at": timestamp,
                "delivered_at": None,
                "failed_at": None,
                "failure_reason": "",
                "claim_alert_id": str(alert["_id"]),
                "idempotency_key": f"client_booking:{booking['_id']}:in_app:claim:{user['_id']}",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            try:
                await db.booking_notifications.insert_one(in_app)
            except DuplicateKeyError:
                pass

        email_notification = {
            "organization_id": product["organization_id"],
            "product_id": str(product["_id"]),
            "booking_id": str(booking["_id"]),
            "recipient_user_id": str(user["_id"]),
            "recipient_email": user.get("email", ""),
            "channel": "email",
            "type": "booking_claim_alert",
            "status": "PENDING_EMAIL_INTEGRATION",
            "provider": settings.email_provider if settings.email_enabled else "disabled",
            "provider_message_id": "",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": "",
            "claim_alert_id": str(alert["_id"]),
            "idempotency_key": f"client_booking:{booking['_id']}:email:claim:{user['_id']}",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = await db.booking_notifications.insert_one(email_notification)
            email_notification["_id"] = result.inserted_id
        except DuplicateKeyError:
            continue
        delivery = await email_service.send_booking_notification(
            BookingNotificationMessage(
                recipient_email=user.get("email", ""),
                recipient_name=user.get("name", "Team member"),
                product_name=product["name"],
                client_name=booking["client_name"],
                client_company=booking.get("client_company", ""),
                issue_category=booking["issue_category"],
                issue_title=booking["issue_title"],
                issue_description=booking.get("issue_description", ""),
                priority=booking["priority"],
                start_time=booking["start_time_utc"],
                end_time=booking["end_time_utc"],
                timezone=booking["product_timezone"],
                duration_minutes=int((booking["end_time_utc"] - booking["start_time_utc"]).total_seconds() // 60),
                booking_link=booking_claim_link(claim_token),
                client_phone=booking.get("client_phone", ""),
                product_reference_number=booking.get("product_reference_number", ""),
                meeting_url="",
                booking_status="pending_approval",
                source_domain=str(booking.get("source_domain") or ""),
                reply_to_email=booking["client_email"] if settings.booking_reply_to_enabled else "",
                notification_id=str(email_notification["_id"]),
                idempotency_key=email_notification["idempotency_key"],
            )
        )
        await update_notification_delivery(email_notification, delivery)
    return alerts


async def list_open_claim_alerts_for_user(product_id: str, user: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_database()
    alerts = [
        item
        async for item in db.booking_claim_alerts.find(
            {
                "product_id": product_id,
                "recipient_user_id": str(user["_id"]),
                "status": "open",
            }
        ).sort("created_at", -1)
    ]
    results: list[dict[str, Any]] = []
    for alert in alerts:
        try:
            booking = await db.client_bookings.find_one({"_id": object_id(alert["booking_id"])})
        except ValueError:
            booking = None
        if booking is None or booking.get("status") != "pending_approval":
            continue
        results.append(
            {
                "id": str(alert["_id"]),
                "product_id": alert["product_id"],
                "booking_id": alert["booking_id"],
                "status": alert.get("status", "open"),
                "audience": alert.get("audience", ""),
                "claim_token": alert.get("claim_token", ""),
                "created_at": alert.get("created_at"),
                "client_name": booking.get("client_name", ""),
                "client_email": booking.get("client_email", ""),
                "client_company": booking.get("client_company", ""),
                "issue_title": booking.get("issue_title", ""),
                "issue_category": booking.get("issue_category", ""),
                "priority": booking.get("priority", ""),
                "start_time": booking.get("start_time_utc"),
                "end_time": booking.get("end_time_utc"),
                "timezone": booking.get("product_timezone", ""),
                "issue_description": booking.get("issue_description", ""),
            }
        )
    return results


async def _close_other_alerts(booking_id: str, winner_alert_id: Any | None, claimed_by: str) -> None:
    timestamp = now_utc()
    db = get_database()
    # Clearing claim_token makes every link for this booking single-use.
    if winner_alert_id is not None:
        await db.booking_claim_alerts.update_one(
            {"_id": winner_alert_id},
            {
                "$set": {
                    "status": "claimed",
                    "claimed_at": timestamp,
                    "claimed_by": claimed_by,
                    "claim_token": "",
                    "updated_at": timestamp,
                }
            },
        )
    losers = {"booking_id": booking_id, "status": "open"}
    if winner_alert_id is not None:
        losers["_id"] = {"$ne": winner_alert_id}
    await db.booking_claim_alerts.update_many(
        losers,
        {"$set": {"status": "closed", "claim_token": "", "updated_at": timestamp}},
    )
    await db.booking_notifications.update_many(
        {"booking_id": booking_id, "type": "booking_claim_alert", "channel": "in_app", "status": "UNREAD"},
        {"$set": {"status": "READ", "updated_at": timestamp}},
    )


async def claim_booking_as_user(product: dict[str, Any], user: dict[str, Any], booking_id: str) -> dict[str, Any]:
    db = get_database()
    product_id = str(product["_id"])
    try:
        booking_oid = object_id(booking_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found") from None

    booking = await db.client_bookings.find_one({"_id": booking_oid, "product_id": product_id})
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.get("status") != "pending_approval":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This request was already accepted or closed")

    alert = await db.booking_claim_alerts.find_one(
        {
            "booking_id": booking_id,
            "product_id": product_id,
            "recipient_user_id": str(user["_id"]),
            "status": "open",
        }
    )
    # Controllers with manage_availability can claim even without a personal alert (backup Approve path stays).
    membership = await db.product_memberships.find_one(
        {"product_id": product_id, "user_id": str(user["_id"]), "status": "active"}
    )
    role = (membership or {}).get("role", "")
    if alert is None and role not in CONTROLLER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have an open claim for this request")

    # First-wins lock on booking
    lock_result = await db.client_bookings.update_one(
        {
            "_id": booking_oid,
            "status": "pending_approval",
            "$or": [
                {"claim_locked_by": {"$exists": False}},
                {"claim_locked_by": ""},
                {"claim_locked_by": None},
            ],
        },
        {
            "$set": {
                "claim_locked_by": str(user["_id"]),
                "claim_locked_at": now_utc(),
                "updated_at": now_utc(),
            }
        },
    )
    # matched_count is the win signal: the filter demands an unlocked pending booking,
    # so a match means this caller set the lock. modified_count would be ambiguous.
    if lock_result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Someone already accepted this request",
        )

    member_id = str(user["_id"])
    try:
        await assign_client_booking(product, user, booking_id, member_id, reason="Accepted via shift claim")
        approved = await approve_client_booking(product, user, booking_id, reason="Accepted via shift claim")
    except Exception:
        # Release the lock, otherwise a failed accept (calendar outage, transient
        # error) would leave the booking permanently unclaimable by anyone.
        await db.client_bookings.update_one(
            {"_id": booking_oid, "claim_locked_by": member_id, "status": "pending_approval"},
            {"$set": {"claim_locked_by": "", "claim_locked_at": None, "updated_at": now_utc()}},
        )
        raise

    # A controller claiming without a personal alert has no winning row to mark, so
    # every open alert is simply closed rather than crediting an unrelated member.
    await _close_other_alerts(booking_id, alert["_id"] if alert is not None else None, str(user["_id"]))

    # Fan-out invite-style notice to verified controller mailboxes after accept
    for email in await verified_controller_emails(product):
        notification = {
            "organization_id": product["organization_id"],
            "product_id": product_id,
            "booking_id": booking_id,
            "recipient_user_id": "",
            "recipient_email": email,
            "channel": "email",
            "type": "controller_booking_claimed",
            "status": "PENDING_EMAIL_INTEGRATION",
            "provider": settings.email_provider if settings.email_enabled else "disabled",
            "provider_message_id": "",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": "",
            "idempotency_key": f"client_booking:{booking_id}:email:claimed:{email}",
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }
        try:
            result = await db.booking_notifications.insert_one(notification)
            notification["_id"] = result.inserted_id
        except DuplicateKeyError:
            continue
        delivery = await email_service.send_booking_notification(
            BookingNotificationMessage(
                recipient_email=email,
                recipient_name=product.get("name", "Workspace"),
                product_name=product["name"],
                client_name=approved["client_name"],
                client_company=approved.get("client_company", ""),
                issue_category=approved["issue_category"],
                issue_title=approved["issue_title"],
                issue_description=approved.get("issue_description", ""),
                priority=approved["priority"],
                start_time=approved["start_time_utc"],
                end_time=approved["end_time_utc"],
                timezone=approved["product_timezone"],
                duration_minutes=int((approved["end_time_utc"] - approved["start_time_utc"]).total_seconds() // 60),
                booking_link=approved.get("google_meet_url") or confirmation_link(approved["secure_token_reference"]),
                client_phone=approved.get("client_phone", ""),
                product_reference_number=approved.get("product_reference_number", ""),
                meeting_url=approved.get("google_meet_url", ""),
                booking_status="scheduled",
                source_domain=str(approved.get("source_domain") or ""),
                reply_to_email="",
                notification_id=str(notification["_id"]),
                idempotency_key=notification["idempotency_key"],
            )
        )
        await update_notification_delivery(notification, delivery)

    return approved


def _ensure_claim_token_live(alert: dict[str, Any]) -> None:
    """Reject a claim link past its window. Legacy alerts have no expiry recorded."""
    expires_at = alert.get("claim_expires_at")
    if expires_at is not None and as_utc(expires_at) <= now_utc():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This claim link has expired",
        )


async def claim_booking_by_token(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid")
    db = get_database()
    alert = await db.booking_claim_alerts.find_one({"claim_token": token})
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid")
    if alert.get("status") != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This request was already accepted or closed")
    _ensure_claim_token_live(alert)
    try:
        product = await db.products.find_one({"_id": object_id(alert["product_id"])})
        user = await db.users.find_one({"_id": object_id(alert["recipient_user_id"])})
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid") from None
    if product is None or user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid")
    return await claim_booking_as_user(product, user, alert["booking_id"])


async def public_claim_preview(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    db = get_database()
    alert = await db.booking_claim_alerts.find_one({"claim_token": token})
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid")
    try:
        booking = await db.client_bookings.find_one({"_id": object_id(alert["booking_id"])})
        product = await db.products.find_one({"_id": object_id(alert["product_id"])})
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid") from None
    if booking is None or product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim link is invalid")
    # The preview stays readable after expiry so the recipient sees why the link is
    # dead; only accepting is blocked.
    claim_expires_at = alert.get("claim_expires_at")
    expired = claim_expires_at is not None and as_utc(claim_expires_at) <= now_utc()
    return {
        "token": token,
        "status": "expired" if expired and alert.get("status") == "open" else alert.get("status", "open"),
        "booking_status": booking.get("status", ""),
        "product_name": product.get("name", ""),
        "client_name": booking.get("client_name", ""),
        "issue_title": booking.get("issue_title", ""),
        "issue_category": booking.get("issue_category", ""),
        "priority": booking.get("priority", ""),
        "start_time": booking.get("start_time_utc"),
        "end_time": booking.get("end_time_utc"),
        "timezone": booking.get("product_timezone", ""),
        "can_accept": (
            alert.get("status") == "open" and booking.get("status") == "pending_approval" and not expired
        ),
    }
