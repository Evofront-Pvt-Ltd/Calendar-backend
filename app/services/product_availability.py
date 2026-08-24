import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import as_utc, now_utc, object_id, public_doc
from app.services.email import BookingConfirmationMessage, BookingNotificationMessage, email_service
from app.services.google_calendar import google_calendar_service
from app.services.scheduling import normalize_timezone, parse_hhmm, timezone_or_400

SUPPORT_ROLES = {"product_owner", "calendar_controller", "member"}


def minutes_from_hhmm(value: str) -> int:
    parsed = parse_hhmm(value)
    return parsed.hour * 60 + parsed.minute


def hhmm_from_minutes(value: int) -> str:
    value = value % (24 * 60)
    return f"{value // 60:02d}:{value % 60:02d}"


def window_duration_minutes(start_time: str, end_time: str) -> int:
    start_minutes = minutes_from_hhmm(start_time)
    end_minutes = minutes_from_hhmm(end_time)
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    return end_minutes - start_minutes


def divide_coverage_minutes(start_time: str, end_time: str, member_count: int) -> list[tuple[str, str]]:
    if member_count <= 0:
        return []
    start_minutes = minutes_from_hhmm(start_time)
    total_minutes = window_duration_minutes(start_time, end_time)
    base_minutes, remainder = divmod(total_minutes, member_count)
    cursor = start_minutes
    windows: list[tuple[str, str]] = []
    for index in range(member_count):
        duration = base_minutes + (1 if index < remainder else 0)
        next_cursor = cursor + duration
        windows.append((hhmm_from_minutes(cursor), hhmm_from_minutes(next_cursor)))
        cursor = next_cursor
    return windows


def slot_signature(raw: str) -> str:
    digest = hmac.new(settings.jwt_secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def b64_json(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def read_b64_json(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding))


def make_slot_key(product_id: str, member_id: str, start_utc: datetime, end_utc: datetime) -> str:
    payload = b64_json(
        {
            "p": product_id,
            "m": member_id,
            "s": as_utc(start_utc).isoformat(),
            "e": as_utc(end_utc).isoformat(),
        }
    )
    return f"{payload}.{slot_signature(payload)}"


def verify_slot_key(slot_key: str) -> dict[str, str]:
    try:
        payload, signature = slot_key.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid slot selection") from None
    if not hmac.compare_digest(signature, slot_signature(payload)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid slot selection")
    try:
        data = read_b64_json(payload)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid slot selection") from None
    required = {"p", "m", "s", "e"}
    if not required.issubset(data):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid slot selection")
    return {key: str(data[key]) for key in required}


def default_policy_from_product(product: dict[str, Any]) -> dict[str, Any]:
    availability = product.get("availability") or {}
    return {
        "organization_id": product["organization_id"],
        "product_id": str(product["_id"]),
        "support_start_time": settings.default_support_start_time,
        "support_end_time": settings.default_support_end_time,
        "timezone": normalize_timezone(
            availability.get("timezone") or settings.default_product_timezone or "Asia/Kolkata"
        ),
        "distribution_mode": "equal_sequential",
        "appointment_duration_minutes": settings.default_appointment_duration_minutes,
        "slot_interval_minutes": int(availability.get("slot_interval_minutes", settings.default_appointment_duration_minutes)),
        "buffer_before_minutes": int(availability.get("buffer_before_minutes", 0)),
        "buffer_after_minutes": int(availability.get("buffer_after_minutes", 0)),
        "minimum_booking_notice_minutes": int(availability.get("min_notice_minutes", 60)),
        "maximum_advance_booking_days": 30,
        "maximum_concurrent_bookings": 1,
        "active": True,
    }


async def ensure_policy(product: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    db = get_database()
    product_id = str(product["_id"])
    policy = await db.availability_policies.find_one({"product_id": product_id})
    if policy is not None:
        return policy
    timestamp = now_utc()
    policy = {
        **default_policy_from_product(product),
        "created_by": str(user["_id"]) if user else product.get("created_by", ""),
        "updated_by": str(user["_id"]) if user else product.get("created_by", ""),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await db.availability_policies.insert_one(policy)
    except DuplicateKeyError:
        return await db.availability_policies.find_one({"product_id": product_id})
    policy["_id"] = result.inserted_id
    return policy


async def audit_availability(
    product: dict[str, Any],
    user: dict[str, Any],
    action: str,
    previous_value: dict | None,
    new_value: dict | None,
    member_id: str = "",
    reason: str = "",
) -> None:
    await get_database().availability_audit_logs.insert_one(
        {
            "organization_id": product["organization_id"],
            "product_id": str(product["_id"]),
            "member_id": member_id,
            "action": action,
            "previous_value": public_doc(previous_value or {}),
            "new_value": public_doc(new_value or {}),
            "changed_by": str(user["_id"]),
            "change_reason": reason,
            "created_at": now_utc(),
        }
    )


async def eligible_product_members(product_id: str) -> list[dict[str, Any]]:
    db = get_database()
    memberships = [
        item
        async for item in db.product_memberships.find({"product_id": product_id, "status": "active"}).sort(
            [("created_at", 1), ("user_id", 1)]
        )
    ]
    eligible: list[dict[str, Any]] = []
    for membership in memberships:
        if membership.get("role") not in SUPPORT_ROLES:
            continue
        try:
            user = await db.users.find_one({"_id": object_id(membership["user_id"])})
        except ValueError:
            user = None
        if user is None:
            continue
        if user.get("status", "active") != "active":
            continue
        eligible.append({"membership": membership, "user": user})
    return eligible


async def product_member_summaries(product_id: str) -> list[dict[str, Any]]:
    db = get_database()
    memberships = [
        item
        async for item in db.product_memberships.find({"product_id": product_id}).sort([("created_at", 1), ("user_id", 1)])
    ]
    summaries: list[dict[str, Any]] = []
    for membership in memberships:
        user = None
        try:
            user = await db.users.find_one({"_id": object_id(membership["user_id"])})
        except ValueError:
            pass
        role = membership.get("role", "member")
        membership_status = membership.get("status", "inactive")
        user_status = user.get("status", "unknown") if user else "missing"
        included = role in SUPPORT_ROLES and membership_status == "active" and user_status == "active"
        reason = ""
        if role == "viewer":
            reason = "Viewer role is not included in support rotation"
        elif membership_status != "active":
            reason = "Membership is inactive"
        elif user_status != "active":
            reason = "User is not active"
        summaries.append(
            {
                "member_id": membership["user_id"],
                "membership_id": str(membership["_id"]),
                "full_name": user.get("name", "Unknown member") if user else "Unknown member",
                "role": role,
                "status": membership_status,
                "included_in_rotation": included,
                "reason": reason,
            }
        )
    return summaries


async def generate_equal_coverage(
    product: dict[str, Any],
    user: dict[str, Any],
    target_date: date,
    preserve_manual_overrides: bool = True,
    force_regenerate: bool = False,
) -> list[dict[str, Any]]:
    policy = await ensure_policy(product, user)
    if policy.get("distribution_mode") not in {"equal_sequential", "manual"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This distribution mode is not implemented yet")
    timezone_or_400(policy["timezone"])
    product_id = str(product["_id"])
    date_key = target_date.isoformat()
    db = get_database()
    manual_records = [
        item
        async for item in db.member_availabilities.find(
            {"product_id": product_id, "date": date_key, "source": "MANUAL"}
        )
    ]
    manual_member_ids = {item["member_id"] for item in manual_records}
    if force_regenerate:
        manual_member_ids = set()

    eligible = await eligible_product_members(product_id)
    windows = divide_coverage_minutes(policy["support_start_time"], policy["support_end_time"], len(eligible))
    await db.member_availabilities.delete_many({"product_id": product_id, "date": date_key, "source": "GENERATED"})
    timestamp = now_utc()
    generated: list[dict[str, Any]] = []
    for member, (start_time, end_time) in zip(eligible, windows):
        member_id = member["membership"]["user_id"]
        if preserve_manual_overrides and member_id in manual_member_ids:
            continue
        record = {
            "organization_id": product["organization_id"],
            "product_id": product_id,
            "member_id": member_id,
            "day_of_week": target_date.weekday(),
            "date": date_key,
            "start_time": start_time,
            "end_time": end_time,
            "timezone": policy["timezone"],
            "recurrence_rule": "",
            "source": "GENERATED",
            "status": "available",
            "effective_from": date_key,
            "effective_until": date_key,
            "created_by": str(user["_id"]),
            "updated_by": str(user["_id"]),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        result = await db.member_availabilities.insert_one(record)
        record["_id"] = result.inserted_id
        generated.append(record)
    await audit_availability(
        product,
        user,
        "generate_equal_coverage",
        {},
        {"date": target_date.isoformat(), "generated_count": len(generated)},
        reason="Generated equal sequential product-team coverage",
    )
    return generated


async def coverage_records_for_date(product: dict[str, Any], user: dict[str, Any], target_date: date) -> list[dict[str, Any]]:
    product_id = str(product["_id"])
    date_key = target_date.isoformat()
    db = get_database()
    eligible = await eligible_product_members(product_id)
    eligible_ids = {item["membership"]["user_id"] for item in eligible}
    policy = await ensure_policy(product, user)
    records = [
        item
        async for item in db.member_availabilities.find({"product_id": product_id, "date": date_key}).sort(
            [("start_time", 1), ("member_id", 1)]
        )
    ]
    generated_ids = {item["member_id"] for item in records if item.get("source") == "GENERATED"}
    manual_exists = any(item.get("source") == "MANUAL" for item in records)
    manual_member_ids = {item["member_id"] for item in records if item.get("source") == "MANUAL"}
    generated_by_member = {item["member_id"]: item for item in records if item.get("source") == "GENERATED"}
    expected_windows = divide_coverage_minutes(policy["support_start_time"], policy["support_end_time"], len(eligible))
    generated_out_of_sync = False
    for member, (start_time, end_time) in zip(eligible, expected_windows):
        member_id = member["membership"]["user_id"]
        if member_id in manual_member_ids:
            continue
        generated = generated_by_member.get(member_id)
        if generated is None or generated.get("start_time") != start_time or generated.get("end_time") != end_time:
            generated_out_of_sync = True
            break
    if not records or (not manual_exists and generated_ids != eligible_ids) or generated_out_of_sync:
        await generate_equal_coverage(product, user, target_date, preserve_manual_overrides=True)
        records = [
            item
            async for item in db.member_availabilities.find({"product_id": product_id, "date": date_key}).sort(
                [("start_time", 1), ("member_id", 1)]
            )
        ]

    manual_member_ids = {item["member_id"] for item in records if item.get("source") == "MANUAL"}
    effective = [
        item
        for item in records
        if item.get("status") == "available"
        and item["member_id"] in eligible_ids
        and (item.get("source") == "MANUAL" or item["member_id"] not in manual_member_ids)
    ]
    return sorted(effective, key=lambda item: (item["start_time"], item["member_id"]))


async def decorate_availability(record: dict[str, Any]) -> dict[str, Any]:
    db = get_database()
    user = None
    membership = None
    try:
        user = await db.users.find_one({"_id": object_id(record["member_id"])})
    except ValueError:
        pass
    membership = await db.product_memberships.find_one(
        {"product_id": record["product_id"], "user_id": record["member_id"]}
    )
    return {
        **public_doc(record),
        "member_name": user.get("name", "Unknown member") if user else "Unknown member",
        "member_role": membership.get("role", "") if membership else "",
    }


def local_window_to_utc(target_date: date, start_time: str, end_time: str, timezone: str) -> tuple[datetime, datetime]:
    tz = timezone_or_400(timezone)
    local_start = datetime.combine(target_date, parse_hhmm(start_time), tzinfo=tz)
    local_end = datetime.combine(target_date, parse_hhmm(end_time), tzinfo=tz)
    if local_end <= local_start:
        local_end += timedelta(days=1)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


def ranges_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and end_a > start_b


async def product_exceptions(product_id: str, target_date: date) -> list[dict[str, Any]]:
    date_key = target_date.isoformat()
    return [
        item
        async for item in get_database().availability_exceptions.find(
            {"product_id": product_id, "exception_date": date_key}
        )
    ]


async def blocks_slot(
    product_id: str,
    member_id: str,
    slot_start: datetime,
    slot_end: datetime,
    target_date: date,
    timezone: str,
    exceptions: list[dict[str, Any]],
) -> bool:
    for exception in exceptions:
        if exception.get("member_id") not in {"", member_id, None}:
            continue
        block_start, block_end = local_window_to_utc(
            target_date,
            exception["start_time"],
            exception["end_time"],
            exception.get("timezone") or timezone,
        )
        if ranges_overlap(slot_start, slot_end, block_start, block_end):
            return True
    return False


async def build_product_slots(
    product: dict[str, Any],
    user: dict[str, Any],
    target_date: date,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    policy = await ensure_policy(product, user)
    if not policy.get("active", True):
        return []
    timezone_or_400(policy["timezone"])
    product_id = str(product["_id"])
    coverage = await coverage_records_for_date(product, user, target_date)
    if minutes_from_hhmm(policy["support_end_time"]) < minutes_from_hhmm(policy["support_start_time"]):
        previous_coverage = await coverage_records_for_date(product, user, target_date - timedelta(days=1))
        coverage = previous_coverage + coverage
    google_busy_by_member: dict[str, list[tuple[datetime, datetime]]] = {}
    if settings.google_calendar_enabled:
        coverage_member_ids = [record["member_id"] for record in coverage]
        connected_member_ids = await google_calendar_service.connected_user_ids(coverage_member_ids)
        coverage = [record for record in coverage if record["member_id"] in connected_member_ids]
        policy_tz = timezone_or_400(policy["timezone"])
        day_start = datetime.combine(target_date, time.min, tzinfo=policy_tz).astimezone(UTC)
        day_end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=policy_tz).astimezone(UTC)
        google_busy_by_member = await google_calendar_service.busy_periods_for_members(
            [record["member_id"] for record in coverage],
            day_start,
            day_end,
            policy["timezone"],
        )
    exceptions = await product_exceptions(product_id, target_date)
    bookings = [
        item
        async for item in get_database().client_bookings.find(
            {"product_id": product_id, "status": "scheduled"},
            {"assigned_member_id": 1, "start_time_utc": 1, "end_time_utc": 1},
        )
    ]
    duration = timedelta(minutes=int(policy["appointment_duration_minutes"]))
    interval = timedelta(minutes=int(policy.get("slot_interval_minutes", policy["appointment_duration_minutes"])))
    buffer_before = timedelta(minutes=int(policy.get("buffer_before_minutes", 0)))
    buffer_after = timedelta(minutes=int(policy.get("buffer_after_minutes", 0)))
    min_start = now_utc() + timedelta(minutes=int(policy.get("minimum_booking_notice_minutes", 0)))
    max_start = now_utc() + timedelta(days=int(policy.get("maximum_advance_booking_days", 30)))
    slots: list[dict[str, Any]] = []
    for record in coverage:
        record_date = date.fromisoformat(str(record.get("date") or target_date.isoformat()))
        local_start_utc, local_end_utc = local_window_to_utc(record_date, record["start_time"], record["end_time"], record["timezone"])
        cursor = local_start_utc
        while cursor + duration <= local_end_utc:
            slot_start = cursor
            slot_end = cursor + duration
            local_label_time = slot_start.astimezone(timezone_or_400(policy["timezone"]))
            if local_label_time.date() != target_date:
                cursor += interval
                continue
            member_bookings = [booking for booking in bookings if booking.get("assigned_member_id") == record["member_id"]]
            overlap_count = sum(
                1
                for booking in member_bookings
                if ranges_overlap(
                    slot_start - buffer_before,
                    slot_end + buffer_after,
                    as_utc(booking["start_time_utc"]),
                    as_utc(booking["end_time_utc"]),
                )
            )
            google_overlap_count = sum(
                1
                for busy_start, busy_end in google_busy_by_member.get(record["member_id"], [])
                if ranges_overlap(
                    slot_start - buffer_before,
                    slot_end + buffer_after,
                    busy_start,
                    busy_end,
                )
            )
            blocked = await blocks_slot(
                product_id,
                record["member_id"],
                slot_start,
                slot_end,
                target_date,
                record["timezone"],
                exceptions,
            )
            if (
                slot_start >= min_start
                and slot_start <= max_start
                and overlap_count < int(policy.get("maximum_concurrent_bookings", 1))
                and google_overlap_count == 0
                and not blocked
            ):
                slot_key = make_slot_key(product_id, record["member_id"], slot_start, slot_end)
                slot = {
                    "start_time_utc": slot_start,
                    "end_time_utc": slot_end,
                    "local_date": local_label_time.date(),
                    "local_time": local_label_time.strftime("%H:%M"),
                    "label": local_label_time.strftime("%I:%M %p").lstrip("0"),
                    "slot_key": slot_key,
                    "source": record.get("source", ""),
                }
                if include_internal:
                    decorated = await decorate_availability(record)
                    slot["member_id"] = record["member_id"]
                    slot["member_name"] = decorated.get("member_name", "")
                slots.append(slot)
            cursor += interval
    return sorted(slots, key=lambda item: (item["start_time_utc"], item.get("member_id", "")))


def confirmation_link(token_reference: str) -> str:
    return f"{settings.application_base_url.rstrip('/')}/support/confirmation/{token_reference}"


async def create_booking_notifications(product: dict[str, Any], booking: dict[str, Any], member: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_database()
    timestamp = now_utc()
    notifications: list[dict[str, Any]] = []
    if settings.in_app_notifications_enabled and settings.notifications_enabled:
        notification = {
            "organization_id": product["organization_id"],
            "product_id": str(product["_id"]),
            "booking_id": str(booking["_id"]),
            "recipient_user_id": str(member["_id"]),
            "recipient_email": member["email"],
            "channel": "in_app",
            "type": "client_booking_created",
            "status": "UNREAD",
            "provider": "in_app",
            "provider_message_id": "",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": timestamp,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": "",
            "idempotency_key": f"client_booking:{booking['_id']}:in_app:{member['_id']}",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = await db.booking_notifications.insert_one(notification)
            notification["_id"] = result.inserted_id
            notifications.append(notification)
        except DuplicateKeyError:
            pass

    email_notification = {
        "organization_id": product["organization_id"],
        "product_id": str(product["_id"]),
        "booking_id": str(booking["_id"]),
        "recipient_user_id": str(member["_id"]),
        "recipient_email": member["email"],
        "channel": "email",
        "type": "client_booking_created",
        "status": "PENDING_EMAIL_INTEGRATION",
        "provider": settings.email_provider if settings.email_enabled else "disabled",
        "provider_message_id": "",
        "attempts": 0,
        "last_attempt_at": None,
        "sent_at": None,
        "delivered_at": None,
        "failed_at": None,
        "failure_reason": "",
        "idempotency_key": f"client_booking:{booking['_id']}:email:{member['_id']}",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await db.booking_notifications.insert_one(email_notification)
        email_notification["_id"] = result.inserted_id
    except DuplicateKeyError:
        return notifications

    email_idempotency_key = email_notification["idempotency_key"]
    delivery = await email_service.send_booking_notification(
        BookingNotificationMessage(
            recipient_email=member["email"],
            recipient_name=member.get("name", "Team member"),
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
            meeting_url=booking.get("google_meet_url", ""),
            booking_status=booking.get("status", "scheduled"),
            reply_to_email=booking["client_email"] if settings.booking_reply_to_enabled else "",
            notification_id=str(email_notification["_id"]),
            idempotency_key=email_idempotency_key,
        )
    )
    updates: dict[str, Any] = {
        "status": delivery.status,
        "provider_message_id": delivery.provider_message_id,
        "failure_reason": delivery.failure_reason,
        "attempts": delivery.attempts,
        "last_attempt_at": now_utc(),
        "updated_at": now_utc(),
    }
    if delivery.status == "DELIVERED":
        updates["delivered_at"] = now_utc()
    if delivery.status == "SENT":
        updates["sent_at"] = delivery.sent_at or now_utc()
    if delivery.status in {"FAILED", "TEMPORARY_FAILURE", "BOUNCED", "DROPPED", "SPAM_REPORT"}:
        updates["failed_at"] = now_utc()
    await db.booking_notifications.update_one({"_id": email_notification["_id"]}, {"$set": updates})
    email_notification.update(updates)
    notifications.append(email_notification)

    client_notification = {
        "organization_id": product["organization_id"],
        "product_id": str(product["_id"]),
        "booking_id": str(booking["_id"]),
        "recipient_user_id": "",
        "recipient_email": booking["client_email"],
        "channel": "email",
        "type": "client_booking_confirmation",
        "status": "PENDING_EMAIL_INTEGRATION",
        "provider": settings.email_provider if settings.email_enabled else "disabled",
        "provider_message_id": "",
        "attempts": 0,
        "last_attempt_at": None,
        "sent_at": None,
        "delivered_at": None,
        "failed_at": None,
        "failure_reason": "",
        "idempotency_key": f"client_booking:{booking['_id']}:email:client",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await db.booking_notifications.insert_one(client_notification)
        client_notification["_id"] = result.inserted_id
    except DuplicateKeyError:
        return notifications

    client_delivery = await email_service.send_booking_confirmation(
        BookingConfirmationMessage(
            recipient_email=booking["client_email"],
            recipient_name=booking["client_name"],
            product_name=product["name"],
            event_title=f"{product['name']} support booking",
            organizer_name=f"{product['name']} Support Team",
            start_time=booking["start_time_utc"],
            end_time=booking["end_time_utc"],
            timezone=booking["product_timezone"],
            location="",
            meeting_url=booking.get("google_meet_url", ""),
            confirmation_link=confirmation_link(booking["secure_token_reference"]),
            notes=booking.get("issue_description", ""),
            notification_id=str(client_notification["_id"]),
            idempotency_key=client_notification["idempotency_key"],
        )
    )
    client_updates: dict[str, Any] = {
        "status": client_delivery.status,
        "provider_message_id": client_delivery.provider_message_id,
        "failure_reason": client_delivery.failure_reason,
        "attempts": client_delivery.attempts,
        "last_attempt_at": now_utc(),
        "updated_at": now_utc(),
    }
    if client_delivery.status == "DELIVERED":
        client_updates["delivered_at"] = now_utc()
    if client_delivery.status in {"FAILED", "TEMPORARY_FAILURE", "BOUNCED", "DROPPED", "SPAM_REPORT"}:
        client_updates["failed_at"] = now_utc()
    await db.booking_notifications.update_one({"_id": client_notification["_id"]}, {"$set": client_updates})
    client_notification.update(client_updates)
    notifications.append(client_notification)

    if settings.booking_notification_email:
        admin_notification = {
            "organization_id": product["organization_id"],
            "product_id": str(product["_id"]),
            "booking_id": str(booking["_id"]),
            "recipient_user_id": "",
            "recipient_email": settings.booking_notification_email,
            "channel": "email",
                "type": "admin_team_connection_booking_created",
            "status": "PENDING_EMAIL_INTEGRATION",
            "provider": settings.email_provider if settings.email_enabled else "disabled",
            "provider_message_id": "",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": "",
            "idempotency_key": f"client_booking:{booking['_id']}:email:admin",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = await db.booking_notifications.insert_one(admin_notification)
            admin_notification["_id"] = result.inserted_id
        except DuplicateKeyError:
            return notifications

        admin_delivery = await email_service.send_booking_notification(
            BookingNotificationMessage(
                recipient_email=settings.booking_notification_email,
                recipient_name=settings.booking_from_name or "Administrator",
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
                meeting_url=booking.get("google_meet_url", ""),
                booking_status=booking.get("status", "scheduled"),
                reply_to_email=booking["client_email"] if settings.booking_reply_to_enabled else "",
                notification_id=str(admin_notification["_id"]),
                idempotency_key=admin_notification["idempotency_key"],
            )
        )
        admin_updates: dict[str, Any] = {
            "status": admin_delivery.status,
            "provider_message_id": admin_delivery.provider_message_id,
            "failure_reason": admin_delivery.failure_reason,
            "attempts": admin_delivery.attempts,
            "last_attempt_at": now_utc(),
            "updated_at": now_utc(),
        }
        if admin_delivery.status == "DELIVERED":
            admin_updates["delivered_at"] = now_utc()
        if admin_delivery.status == "SENT":
            admin_updates["sent_at"] = admin_delivery.sent_at or now_utc()
        if admin_delivery.status in {"FAILED", "TEMPORARY_FAILURE", "BOUNCED", "DROPPED", "SPAM_REPORT"}:
            admin_updates["failed_at"] = now_utc()
        await db.booking_notifications.update_one({"_id": admin_notification["_id"]}, {"$set": admin_updates})
        admin_notification.update(admin_updates)
        notifications.append(admin_notification)
    return notifications


async def create_client_booking(product: dict[str, Any], payload: Any) -> dict[str, Any]:
    timezone_or_400(payload.client_timezone)
    slot_payload = verify_slot_key(payload.slot_key)
    product_id = str(product["_id"])
    if slot_payload["p"] != product_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid slot selection")
    member_id = slot_payload["m"]
    try:
        member = await get_database().users.find_one({"_id": object_id(member_id)})
    except ValueError:
        member = None
    membership = await get_database().product_memberships.find_one(
        {"product_id": product_id, "user_id": member_id, "status": "active"}
    )
    if member is None or membership is None or membership.get("role") not in SUPPORT_ROLES or member.get("status") != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That team member is no longer available")
    if settings.google_calendar_enabled:
        await google_calendar_service.ensure_member_connected_for_booking(member_id)

    policy = await ensure_policy(product, member)
    requested_start = as_utc(datetime.fromisoformat(slot_payload["s"]))
    requested_end = as_utc(datetime.fromisoformat(slot_payload["e"]))
    lookup_date = requested_start.astimezone(timezone_or_400(policy["timezone"])).date()
    slots = await build_product_slots(product, member, lookup_date, include_internal=True)
    slot = next(
        (
            item
            for item in slots
            if item["member_id"] == member_id
            and as_utc(item["start_time_utc"]) == requested_start
            and as_utc(item["end_time_utc"]) == requested_end
        ),
        None,
    )
    if slot is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That time is no longer available")

    timestamp = now_utc()
    secure_token = secrets.token_urlsafe(24)
    booking = {
        "organization_id": product["organization_id"],
        "product_id": product_id,
        "assigned_member_id": member_id,
        "client_name": payload.client_name.strip(),
        "client_email": str(payload.client_email).lower(),
        "client_phone": getattr(payload, "client_phone", "").strip(),
        "client_company": payload.client_company.strip(),
        "product_reference_number": getattr(payload, "product_reference_number", "").strip(),
        "issue_category": payload.issue_category.strip(),
        "issue_title": payload.issue_title.strip(),
        "issue_description": payload.issue_description.strip(),
        "priority": payload.priority,
        "start_time_utc": requested_start,
        "end_time_utc": requested_end,
        "client_timezone": normalize_timezone(payload.client_timezone),
        "product_timezone": policy["timezone"],
        "status": "scheduled",
        "assignment_strategy": "direct_coverage",
        "assignment_reason": "Slot belongs to the assigned product-team member coverage window",
        "public_booking_reference": secrets.token_urlsafe(10),
        "secure_token_reference": secure_token,
        "organizer_user_id": member_id,
        "additional_attendees": [],
        "google_calendar_id": settings.google_calendar_id or "primary",
        "google_event_id": "",
        "google_meet_url": "",
        "google_event_url": "",
        "google_sync_status": "PENDING" if settings.google_calendar_enabled else "DISABLED",
        "google_conference_status": "",
        "google_synced_at": None,
        "google_sync_error": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await get_database().client_bookings.insert_one(booking)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That time was just booked") from None
    booking["_id"] = result.inserted_id
    if settings.google_calendar_enabled:
        try:
            google_result = await google_calendar_service.create_meet_event_for_booking(
                organizer_user_id=member_id,
                product=product,
                booking=booking,
                attendee_emails=[booking["client_email"]],
            )
        except HTTPException as exc:
            cancelled_at = now_utc()
            await get_database().client_bookings.update_one(
                {"_id": booking["_id"]},
                {
                    "$set": {
                        "status": "cancelled",
                        "google_sync_status": "FAILED",
                        "google_sync_error": "Google Calendar event creation failed",
                        "cancelled_at": cancelled_at,
                        "updated_at": cancelled_at,
                    }
                },
            )
            raise exc
        google_updates = {
            "google_calendar_id": google_result.calendar_id,
            "google_event_id": google_result.event_id,
            "google_meet_url": google_result.meet_url,
            "google_event_url": google_result.event_url,
            "google_sync_status": google_result.sync_status,
            "google_conference_status": google_result.conference_status,
            "google_synced_at": now_utc(),
            "google_sync_error": "",
            "updated_at": now_utc(),
        }
        await get_database().client_bookings.update_one({"_id": booking["_id"]}, {"$set": google_updates})
        booking.update(google_updates)
    await create_booking_notifications(product, booking, member)
    return booking


async def client_booking_to_out(booking: dict[str, Any]) -> dict[str, Any]:
    member = None
    try:
        member = await get_database().users.find_one({"_id": object_id(booking["assigned_member_id"])})
    except ValueError:
        pass
    return {
        **public_doc(booking),
        "assigned_member_name": member.get("name", "") if member else "",
        "confirmation_link": confirmation_link(booking["secure_token_reference"]),
    }


async def cancel_client_booking(
    product: dict[str, Any],
    user: dict[str, Any],
    booking_id: str,
    reason: str = "",
    permissions: set[str] | None = None,
) -> dict[str, Any]:
    try:
        oid = object_id(booking_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found") from None
    db = get_database()
    booking = await db.client_bookings.find_one({"_id": oid, "product_id": str(product["_id"])})
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if "manage_availability" not in (permissions or set()) and booking.get("assigned_member_id") != str(user["_id"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot cancel this booking")
    if booking.get("status") == "cancelled":
        return booking

    if settings.google_calendar_enabled and booking.get("google_event_id"):
        try:
            await google_calendar_service.delete_event(
                booking.get("organizer_user_id") or booking["assigned_member_id"],
                booking.get("google_calendar_id") or settings.google_calendar_id or "primary",
                booking["google_event_id"],
            )
        except HTTPException:
            await db.client_bookings.update_one(
                {"_id": booking["_id"]},
                {
                    "$set": {
                        "google_sync_status": "CANCEL_FAILED",
                        "google_sync_error": "Google Calendar cancellation failed",
                        "updated_at": now_utc(),
                    }
                },
            )
            raise

    timestamp = now_utc()
    updates = {
        "status": "cancelled",
        "cancellation_reason": reason.strip(),
        "cancelled_at": timestamp,
        "google_sync_status": "CANCELLED" if booking.get("google_event_id") else booking.get("google_sync_status", "DISABLED"),
        "updated_at": timestamp,
    }
    await db.client_bookings.update_one({"_id": booking["_id"]}, {"$set": updates})
    booking.update(updates)
    return booking


async def team_availability_context(product: dict[str, Any], user: dict[str, Any], target_date: date) -> dict[str, Any]:
    date_key = target_date.isoformat()
    policy = await ensure_policy(product, user)
    coverage = [await decorate_availability(item) for item in await coverage_records_for_date(product, user, target_date)]
    slots = await build_product_slots(product, user, target_date, include_internal=True)
    bookings = [
        await client_booking_to_out(item)
        async for item in get_database().client_bookings.find(
            {"product_id": str(product["_id"]), "start_time_utc": {"$gte": datetime.combine(target_date, time.min, tzinfo=UTC)}}
        ).sort("start_time_utc", 1)
    ]
    booking_ids = [item["id"] for item in bookings]
    notifications = [
        public_doc(item)
        async for item in get_database().booking_notifications.find({"booking_id": {"$in": booking_ids}}).sort("created_at", -1)
    ] if booking_ids else []
    exceptions = [
        public_doc(item)
        async for item in get_database().availability_exceptions.find(
            {"product_id": str(product["_id"]), "exception_date": date_key}
        ).sort("start_time", 1)
    ]
    return {
        "product_id": str(product["_id"]),
        "product_name": product["name"],
        "date": target_date,
        "timezone": policy["timezone"],
        "policy": public_doc(policy),
        "members": await product_member_summaries(str(product["_id"])),
        "coverage": coverage,
        "available_slots": slots,
        "bookings": bookings,
        "notifications": notifications,
        "exceptions": exceptions,
    }
