from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status

from app.core.utils import as_utc, now_utc


DEFAULT_WINDOWS = [
    {"day": 0, "start": "00:00", "end": "00:00", "enabled": True},
    {"day": 1, "start": "00:00", "end": "00:00", "enabled": True},
    {"day": 2, "start": "00:00", "end": "00:00", "enabled": True},
    {"day": 3, "start": "00:00", "end": "00:00", "enabled": True},
    {"day": 4, "start": "00:00", "end": "00:00", "enabled": True},
    {"day": 5, "start": "00:00", "end": "00:00", "enabled": True},
    {"day": 6, "start": "00:00", "end": "00:00", "enabled": True},
]

TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Calcutta": "Asia/Kolkata",
    "Etc/UTC": "UTC",
}


def normalize_timezone(name: str) -> str:
    return TIMEZONE_ALIASES.get(name, name)


def default_availability(timezone: str) -> dict:
    timezone = normalize_timezone(timezone)
    return {
        "timezone": timezone,
        "windows": [window.copy() for window in DEFAULT_WINDOWS],
        "min_notice_minutes": 60,
        "slot_interval_minutes": 30,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 0,
    }


def timezone_or_400(name: str) -> ZoneInfo:
    normalized = normalize_timezone(name)
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown timezone") from None


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def local_window_bounds(target_date: date, start: str, end: str, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    local_start = datetime.combine(target_date, parse_hhmm(start), tzinfo=timezone)
    local_end = datetime.combine(target_date, parse_hhmm(end), tzinfo=timezone)
    # Equal start/end represents a full-day shift; earlier end represents an overnight shift.
    if local_end <= local_start:
        local_end += timedelta(days=1)
    return local_start, local_end


def normalize_booking_window(booking: dict) -> tuple[datetime, datetime]:
    return as_utc(booking["start_utc"]), as_utc(booking["end_utc"])


def overlaps(
    start_utc: datetime,
    end_utc: datetime,
    booking: dict,
    buffer_before: int,
    buffer_after: int,
) -> bool:
    booked_start, booked_end = normalize_booking_window(booking)
    blocked_start = booked_start - timedelta(minutes=buffer_before)
    blocked_end = booked_end + timedelta(minutes=buffer_after)
    return start_utc < blocked_end and end_utc > blocked_start


def build_slots(
    availability: dict,
    event_type: dict,
    bookings: list[dict],
    start_date: date,
    end_date: date,
) -> list[dict]:
    if end_date < start_date:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="end_date must be after start_date")
    if (end_date - start_date).days > 60:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slot lookup is limited to 60 days")

    tz = timezone_or_400(availability["timezone"])
    duration = timedelta(minutes=int(event_type["duration_minutes"]))
    interval = timedelta(minutes=int(availability.get("slot_interval_minutes", 30)))
    min_start = now_utc() + timedelta(minutes=int(availability.get("min_notice_minutes", 0)))
    buffer_before = int(availability.get("buffer_before_minutes", 0))
    buffer_after = int(availability.get("buffer_after_minutes", 0))

    windows_by_day: dict[int, list[dict]] = {}
    for window in availability.get("windows", []):
        if window.get("enabled"):
            windows_by_day.setdefault(int(window["day"]), []).append(window)

    slots: list[dict] = []
    current = start_date - timedelta(days=1)
    while current <= end_date:
        for window in windows_by_day.get(current.weekday(), []):
            local_start, local_end = local_window_bounds(current, window["start"], window["end"], tz)
            cursor = local_start
            while cursor + duration <= local_end:
                slot_start = cursor.astimezone(UTC)
                slot_end = (cursor + duration).astimezone(UTC)
                if (
                    start_date <= cursor.date() <= end_date
                    and slot_start >= min_start
                    and not any(
                        overlaps(slot_start, slot_end, booking, buffer_before, buffer_after) for booking in bookings
                    )
                ):
                    slots.append(
                        {
                            "start_utc": slot_start,
                            "end_utc": slot_end,
                            "local_date": cursor.date(),
                            "local_time": cursor.strftime("%H:%M"),
                            "label": cursor.strftime("%I:%M %p").lstrip("0"),
                        }
                    )
                cursor += interval
        current += timedelta(days=1)
    return slots


def requested_slot_is_available(slots: list[dict], requested_start: datetime) -> dict | None:
    requested = as_utc(requested_start)
    for slot in slots:
        if as_utc(slot["start_utc"]) == requested:
            return slot
    return None
