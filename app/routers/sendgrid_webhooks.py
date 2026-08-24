import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import now_utc, object_id
from app.services.sendgrid import (
    map_sendgrid_event_status,
    sendgrid_custom_arg,
    sendgrid_event_identifier,
    sendgrid_message_ids,
    verify_sendgrid_event_signature,
)

router = APIRouter(prefix="/api/webhooks/sendgrid", tags=["sendgrid webhooks"])

SIGNATURE_HEADER = "X-Twilio-Email-Event-Webhook-Signature"
TIMESTAMP_HEADER = "X-Twilio-Email-Event-Webhook-Timestamp"
FAILED_STATUSES = {"BOUNCED", "DROPPED", "SPAM_REPORT", "UNSUBSCRIBED"}


def event_timestamp(event: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(event.get("timestamp", 0)), UTC)
    except (TypeError, ValueError, OSError):
        return now_utc()


def status_update_fields(status_value: str, event: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "updated_at": now_utc(),
        "last_provider_event": status_value,
        "last_provider_event_at": timestamp,
    }
    reason = str(event.get("reason") or event.get("response") or "")
    if status_value == "DELIVERED":
        updates["delivered_at"] = timestamp
        updates["failure_reason"] = ""
    elif status_value == "DEFERRED":
        updates["failure_reason"] = reason[:500]
    elif status_value in FAILED_STATUSES:
        updates["failed_at"] = timestamp
        updates["failure_reason"] = reason[:500] or status_value
    return updates


def invitation_status(status_value: str) -> str:
    if status_value == "DELIVERED":
        return "delivered"
    if status_value == "DEFERRED":
        return "deferred"
    if status_value in FAILED_STATUSES:
        return "email_failed"
    return "queued"


async def update_meeting_invitation(event: dict[str, Any], status_value: str, timestamp: datetime) -> bool:
    db = get_database()
    updates = status_update_fields(status_value, event, timestamp)
    updates["email_delivery_status"] = status_value
    updates["invitation_status"] = invitation_status(status_value)
    message_ids = sendgrid_message_ids(event)
    if message_ids:
        updates["last_provider_message_id"] = message_ids[0]

    invitation_id = sendgrid_custom_arg(event, "invitation_id")
    if invitation_id:
        try:
            result = await db.meeting_invitations.update_one({"_id": object_id(invitation_id)}, {"$set": updates})
            if result.modified_count:
                return True
        except ValueError:
            pass

    if message_ids:
        result = await db.meeting_invitations.update_many(
            {"provider_message_id": {"$in": message_ids}},
            {"$set": updates},
        )
        return bool(result.modified_count)
    return False


async def update_booking_notification(event: dict[str, Any], status_value: str, timestamp: datetime) -> bool:
    db = get_database()
    updates = status_update_fields(status_value, event, timestamp)
    updates["status"] = status_value
    message_ids = sendgrid_message_ids(event)
    if message_ids:
        updates["last_provider_message_id"] = message_ids[0]

    notification_id = sendgrid_custom_arg(event, "notification_id")
    if notification_id:
        try:
            result = await db.booking_notifications.update_one({"_id": object_id(notification_id)}, {"$set": updates})
            if result.modified_count:
                return True
        except ValueError:
            pass

    if message_ids:
        result = await db.booking_notifications.update_many(
            {"provider_message_id": {"$in": message_ids}},
            {"$set": updates},
        )
        return bool(result.modified_count)
    return False


async def apply_sendgrid_event(event: dict[str, Any]) -> bool:
    status_value = map_sendgrid_event_status(str(event.get("event") or ""))
    timestamp = event_timestamp(event)
    record_type = sendgrid_custom_arg(event, "record_type")
    if record_type == "meeting_invitation":
        return await update_meeting_invitation(event, status_value, timestamp)
    if record_type == "booking_notification":
        return await update_booking_notification(event, status_value, timestamp)

    invitation_updated = await update_meeting_invitation(event, status_value, timestamp)
    notification_updated = await update_booking_notification(event, status_value, timestamp)
    return invitation_updated or notification_updated


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def sendgrid_event_webhook(request: Request) -> dict[str, int | str]:
    if not settings.sendgrid_event_webhook_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook is not enabled")

    payload = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")
    timestamp = request.headers.get(TIMESTAMP_HEADER, "")
    if not signature or not timestamp:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing SendGrid webhook signature")
    if not verify_sendgrid_event_signature(payload, signature, timestamp):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid SendGrid webhook signature")

    try:
        events = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid SendGrid webhook payload") from None
    if not isinstance(events, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SendGrid webhook payload must be a list")

    processed = 0
    matched = 0
    db = get_database()
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = sendgrid_event_identifier(event)
        message_ids = sendgrid_message_ids(event)
        status_value = map_sendgrid_event_status(str(event.get("event") or ""))
        event_doc = {
            "sg_event_id": event_id,
            "sg_message_id": message_ids[0] if message_ids else "",
            "event": status_value,
            "record_type": sendgrid_custom_arg(event, "record_type"),
            "reason": str(event.get("reason") or event.get("response") or "")[:500],
            "event_at": event_timestamp(event),
            "created_at": now_utc(),
        }
        try:
            await db.sendgrid_events.insert_one(event_doc)
        except DuplicateKeyError:
            continue
        processed += 1
        if await apply_sendgrid_event(event):
            matched += 1

    return {"status": "accepted", "received": len(events), "processed": processed, "matched": matched}
