import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.database import get_database
from app.core.utils import as_utc, now_utc, object_id

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
GOOGLE_PROVIDER = "google_calendar"


@dataclass(frozen=True)
class GoogleCalendarEventResult:
    calendar_id: str
    event_id: str
    meet_url: str
    event_url: str
    organizer_user_id: str
    attendees: list[str]
    conference_status: str
    sync_status: str


def hash_google_oauth_state(state: str) -> str:
    return hmac.new(settings.jwt_secret.encode("utf-8"), state.encode("utf-8"), hashlib.sha256).hexdigest()


def parse_google_datetime(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def extract_google_meet_url(event: dict[str, Any]) -> str:
    hangout_link = str(event.get("hangoutLink") or "")
    if hangout_link:
        return hangout_link
    conference_data = event.get("conferenceData") or {}
    for entry in conference_data.get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return str(entry["uri"])
    return ""


def google_conference_status(event: dict[str, Any]) -> str:
    conference_data = event.get("conferenceData") or {}
    create_request = conference_data.get("createRequest") or {}
    status_payload = create_request.get("status") or {}
    return str(status_payload.get("statusCode") or "")


def event_time_payload(value: datetime, timezone: str) -> dict[str, str]:
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
        timezone = "UTC"
    local_value = as_utc(value).astimezone(tz)
    return {"dateTime": local_value.isoformat(), "timeZone": timezone}


def build_google_event_request(
    *,
    summary: str,
    description: str,
    start_time: datetime,
    end_time: datetime,
    timezone: str,
    attendee_emails: list[str],
    request_id: str,
    internal_booking_id: str,
    product_id: str,
) -> dict[str, Any]:
    attendees = [{"email": email} for email in sorted({email.strip().lower() for email in attendee_emails if email.strip()})]
    return {
        "summary": summary,
        "description": description,
        "start": event_time_payload(start_time, timezone),
        "end": event_time_payload(end_time, timezone),
        "attendees": attendees,
        "conferenceData": {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "extendedProperties": {
            "private": {
                "calendarBookingId": internal_booking_id,
                "productId": product_id,
            }
        },
    }


class GoogleCalendarService:
    def ensure_configured(self) -> None:
        if not settings.google_calendar_enabled:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google Calendar is disabled")
        if not settings.google_calendar_configured:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google Calendar is not configured")
        self._fernet()

    def _fernet(self) -> Fernet:
        try:
            return Fernet(settings.google_token_encryption_key.encode("utf-8"))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google token encryption key is invalid",
            ) from None

    def encrypt_token(self, token: str) -> str:
        self.ensure_configured()
        return self._fernet().encrypt(token.encode("utf-8")).decode("utf-8")

    def decrypt_token(self, encrypted_token: str) -> str:
        self.ensure_configured()
        try:
            return self._fernet().decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Stored Google token cannot be decrypted",
            ) from None

    async def create_authorization_url(self, user: dict[str, Any]) -> str:
        self.ensure_configured()
        state = secrets.token_urlsafe(32)
        timestamp = now_utc()
        scopes = settings.google_calendar_scope_list
        await get_database().google_oauth_states.insert_one(
            {
                "state_hash": hash_google_oauth_state(state),
                "user_id": str(user["_id"]),
                "organization_id": user.get("organization_id", settings.organization_id),
                "scopes": scopes,
                "used_at": None,
                "expires_at": timestamp + timedelta(minutes=settings.google_oauth_state_ttl_minutes),
                "created_at": timestamp,
            }
        )
        query = urlencode(
            {
                "client_id": settings.google_client_id,
                "redirect_uri": settings.google_redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
            }
        )
        return f"{GOOGLE_AUTH_URL}?{query}"

    async def consume_oauth_state(self, state: str) -> dict[str, Any]:
        if not state:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing OAuth state")
        db = get_database()
        timestamp = now_utc()
        state_doc = await db.google_oauth_states.find_one(
            {"state_hash": hash_google_oauth_state(state), "used_at": None}
        )
        if state_doc is None or as_utc(state_doc["expires_at"]) <= timestamp:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OAuth state")
        update_result = await db.google_oauth_states.update_one(
            {"_id": state_doc["_id"], "used_at": None},
            {"$set": {"used_at": timestamp}},
        )
        if update_result.modified_count != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth state was already used")
        return state_doc

    async def complete_oauth_callback(self, code: str, state: str) -> dict[str, Any]:
        self.ensure_configured()
        state_doc = await self.consume_oauth_state(state)
        if not code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing authorization code")
        token_payload = await self._exchange_code(code)
        return await self._store_connection(state_doc, token_payload)

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.google_api_timeout_seconds) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            logger.warning("Google OAuth token exchange failed", extra={"status_code": response.status_code})
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google authorization failed")
        token_payload = response.json()
        if not token_payload.get("access_token"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google authorization did not return an access token")
        return token_payload

    async def _store_connection(self, state_doc: dict[str, Any], token_payload: dict[str, Any]) -> dict[str, Any]:
        db = get_database()
        user_id = state_doc["user_id"]
        try:
            user = await db.users.find_one({"_id": object_id(user_id)})
        except ValueError:
            user = None
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="OAuth user no longer exists")

        existing = await db.google_calendar_connections.find_one({"user_id": user_id, "provider": GOOGLE_PROVIDER})
        encrypted_refresh_token = ""
        if token_payload.get("refresh_token"):
            encrypted_refresh_token = self.encrypt_token(str(token_payload["refresh_token"]))
        elif existing and existing.get("encrypted_refresh_token"):
            encrypted_refresh_token = existing["encrypted_refresh_token"]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google did not return offline calendar access. Reconnect and approve access again.",
            )

        timestamp = now_utc()
        expires_in = int(token_payload.get("expires_in") or 3600)
        granted_scopes = str(token_payload.get("scope") or " ".join(state_doc.get("scopes", []))).split()
        provider_email = str(user.get("email") or "")
        connection = {
            "user_id": user_id,
            "organization_id": user.get("organization_id", settings.organization_id),
            "provider": GOOGLE_PROVIDER,
            "provider_account_id": provider_email or user_id,
            "provider_email": provider_email,
            "calendar_id": settings.google_calendar_id or "primary",
            "encrypted_access_token": self.encrypt_token(str(token_payload["access_token"])),
            "encrypted_refresh_token": encrypted_refresh_token,
            "token_expiry": timestamp + timedelta(seconds=expires_in),
            "granted_scopes": granted_scopes,
            "connection_status": "connected",
            "last_sync_at": timestamp,
            "last_error_code": "",
            "last_error_message": "",
            "updated_at": timestamp,
        }
        await db.google_calendar_connections.update_one(
            {"user_id": user_id, "provider": GOOGLE_PROVIDER},
            {"$set": connection, "$setOnInsert": {"created_at": timestamp}},
            upsert=True,
        )
        logger.info("Google Calendar connected", extra={"user_id": user_id})
        saved = await db.google_calendar_connections.find_one({"user_id": user_id, "provider": GOOGLE_PROVIDER})
        return saved or connection

    async def connection_for_user(self, user_id: str, require_connected: bool = True) -> dict[str, Any] | None:
        query: dict[str, Any] = {"user_id": user_id, "provider": GOOGLE_PROVIDER}
        if require_connected:
            query["connection_status"] = "connected"
        connection = await get_database().google_calendar_connections.find_one(query)
        if connection is None and require_connected:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Google Calendar is not connected")
        return connection

    async def connected_user_ids(self, user_ids: list[str]) -> set[str]:
        if not settings.google_calendar_enabled or not user_ids:
            return set(user_ids)
        if not settings.google_calendar_configured:
            return set()
        cursor = get_database().google_calendar_connections.find(
            {
                "user_id": {"$in": sorted(set(user_ids))},
                "provider": GOOGLE_PROVIDER,
                "connection_status": "connected",
            },
            {"user_id": 1},
        )
        return {item["user_id"] async for item in cursor}

    async def status_for_user(self, user: dict[str, Any]) -> dict[str, Any]:
        connection = await get_database().google_calendar_connections.find_one(
            {"user_id": str(user["_id"]), "provider": GOOGLE_PROVIDER}
        )
        return {
            "enabled": settings.google_calendar_enabled,
            "configured": settings.google_calendar_configured,
            "connected": bool(connection and connection.get("connection_status") == "connected"),
            "connection_status": connection.get("connection_status", "not_connected") if connection else "not_connected",
            "provider_email": connection.get("provider_email", "") if connection else "",
            "calendar_id": connection.get("calendar_id", settings.google_calendar_id or "primary") if connection else settings.google_calendar_id,
            "granted_scopes": connection.get("granted_scopes", []) if connection else [],
            "token_expiry": connection.get("token_expiry") if connection else None,
            "last_sync_at": connection.get("last_sync_at") if connection else None,
            "last_error_code": connection.get("last_error_code", "") if connection else "",
            "last_error_message": connection.get("last_error_message", "") if connection else "",
        }

    async def disconnect_user(self, user: dict[str, Any]) -> None:
        timestamp = now_utc()
        await get_database().google_calendar_connections.update_one(
            {"user_id": str(user["_id"]), "provider": GOOGLE_PROVIDER},
            {
                "$set": {
                    "connection_status": "disconnected",
                    "encrypted_access_token": "",
                    "encrypted_refresh_token": "",
                    "last_error_code": "",
                    "last_error_message": "",
                    "disconnected_at": timestamp,
                    "updated_at": timestamp,
                }
            },
        )
        logger.info("Google Calendar disconnected", extra={"user_id": str(user["_id"])})

    async def ensure_member_connected_for_booking(self, user_id: str) -> dict[str, Any]:
        self.ensure_configured()
        return await self.connection_for_user(user_id, require_connected=True)

    async def _valid_access_token(self, user_id: str) -> tuple[str, dict[str, Any]]:
        self.ensure_configured()
        connection = await self.connection_for_user(user_id, require_connected=True)
        assert connection is not None
        expiry = connection.get("token_expiry")
        if expiry is None or as_utc(expiry) <= now_utc() + timedelta(minutes=2):
            connection = await self._refresh_connection_token(connection)
        encrypted_access_token = connection.get("encrypted_access_token", "")
        if not encrypted_access_token:
            connection = await self._refresh_connection_token(connection)
            encrypted_access_token = connection.get("encrypted_access_token", "")
        return self.decrypt_token(encrypted_access_token), connection

    async def _refresh_connection_token(self, connection: dict[str, Any]) -> dict[str, Any]:
        refresh_token = self.decrypt_token(connection.get("encrypted_refresh_token", ""))
        async with httpx.AsyncClient(timeout=settings.google_api_timeout_seconds) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            await self._mark_connection_error(connection, "token_refresh_failed", "Google Calendar authorization needs reconnecting")
            logger.warning(
                "Google token refresh failed",
                extra={"user_id": connection.get("user_id"), "status_code": response.status_code},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Google Calendar authorization needs reconnecting")
        payload = response.json()
        timestamp = now_utc()
        updates = {
            "encrypted_access_token": self.encrypt_token(str(payload["access_token"])),
            "token_expiry": timestamp + timedelta(seconds=int(payload.get("expires_in") or 3600)),
            "connection_status": "connected",
            "last_sync_at": timestamp,
            "last_error_code": "",
            "last_error_message": "",
            "updated_at": timestamp,
        }
        if payload.get("scope"):
            updates["granted_scopes"] = str(payload["scope"]).split()
        await get_database().google_calendar_connections.update_one({"_id": connection["_id"]}, {"$set": updates})
        connection.update(updates)
        return connection

    async def _mark_connection_error(self, connection: dict[str, Any], code: str, message: str) -> None:
        await get_database().google_calendar_connections.update_one(
            {"_id": connection["_id"]},
            {
                "$set": {
                    "connection_status": "error",
                    "last_error_code": code,
                    "last_error_message": message,
                    "updated_at": now_utc(),
                }
            },
        )

    async def busy_periods_for_member(
        self,
        user_id: str,
        time_min_utc: datetime,
        time_max_utc: datetime,
        timezone: str,
    ) -> list[tuple[datetime, datetime]]:
        access_token, connection = await self._valid_access_token(user_id)
        calendar_id = connection.get("calendar_id") or settings.google_calendar_id or "primary"
        payload = {
            "timeMin": as_utc(time_min_utc).isoformat().replace("+00:00", "Z"),
            "timeMax": as_utc(time_max_utc).isoformat().replace("+00:00", "Z"),
            "timeZone": timezone,
            "items": [{"id": calendar_id}],
        }
        async with httpx.AsyncClient(timeout=settings.google_api_timeout_seconds) as client:
            response = await client.post(
                f"{GOOGLE_CALENDAR_API_URL}/freeBusy",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            await self._mark_connection_error(connection, "freebusy_failed", "Google busy-time lookup failed")
            logger.warning(
                "Google FreeBusy lookup failed",
                extra={"user_id": user_id, "status_code": response.status_code},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Google busy-time lookup failed")
        data = response.json()
        busy = data.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        return [(parse_google_datetime(item["start"]), parse_google_datetime(item["end"])) for item in busy]

    async def busy_periods_for_members(
        self,
        user_ids: list[str],
        time_min_utc: datetime,
        time_max_utc: datetime,
        timezone: str,
    ) -> dict[str, list[tuple[datetime, datetime]]]:
        if not settings.google_calendar_enabled or not settings.google_calendar_configured:
            return {}
        connected_ids = await self.connected_user_ids(user_ids)
        busy_by_member: dict[str, list[tuple[datetime, datetime]]] = {}
        for user_id in sorted(connected_ids):
            try:
                busy_by_member[user_id] = await self.busy_periods_for_member(user_id, time_min_utc, time_max_utc, timezone)
            except HTTPException:
                busy_by_member[user_id] = [(as_utc(time_min_utc), as_utc(time_max_utc))]
        return busy_by_member

    async def create_meet_event_for_booking(
        self,
        *,
        organizer_user_id: str,
        product: dict[str, Any],
        booking: dict[str, Any],
        attendee_emails: list[str],
    ) -> GoogleCalendarEventResult:
        access_token, connection = await self._valid_access_token(organizer_user_id)
        calendar_id = connection.get("calendar_id") or settings.google_calendar_id or "primary"
        request_id = f"booking-{booking['_id']}-{secrets.token_hex(8)}"
        summary = booking.get("issue_title") or f"{product['name']} support booking"
        description_parts = [
            f"Product: {product['name']}",
            f"Client: {booking['client_name']}",
            f"Company: {booking.get('client_company', '')}",
            f"Category: {booking['issue_category']}",
            f"Priority: {booking['priority']}",
            "",
            booking.get("issue_description", ""),
        ]
        request_body = build_google_event_request(
            summary=summary,
            description="\n".join(part for part in description_parts if part is not None),
            start_time=booking["start_time_utc"],
            end_time=booking["end_time_utc"],
            timezone=booking["product_timezone"],
            attendee_emails=attendee_emails,
            request_id=request_id,
            internal_booking_id=str(booking["_id"]),
            product_id=str(product["_id"]),
        )
        event = await self._insert_google_event(access_token, calendar_id, request_body)
        event_id = str(event.get("id") or "")
        meet_url = extract_google_meet_url(event)
        conference_status = google_conference_status(event)
        if event_id and not meet_url:
            event, meet_url, conference_status = await self._wait_for_meet_link(access_token, calendar_id, event_id)
        if not event_id:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google Calendar did not return an event ID")
        return GoogleCalendarEventResult(
            calendar_id=calendar_id,
            event_id=event_id,
            meet_url=meet_url,
            event_url=str(event.get("htmlLink") or ""),
            organizer_user_id=organizer_user_id,
            attendees=[item["email"] for item in request_body["attendees"]],
            conference_status=conference_status,
            sync_status="SYNCED" if meet_url else "MEET_LINK_PENDING",
        )

    async def create_meet_event(
        self,
        *,
        organizer_user_id: str,
        product_id: str,
        title: str,
        description: str,
        start_time: datetime,
        end_time: datetime,
        timezone: str,
        attendee_emails: list[str],
        internal_record_id: str,
        request_prefix: str = "meeting",
    ) -> GoogleCalendarEventResult:
        access_token, connection = await self._valid_access_token(organizer_user_id)
        calendar_id = connection.get("calendar_id") or settings.google_calendar_id or "primary"
        request_id = f"{request_prefix}-{internal_record_id}-{secrets.token_hex(8)}"
        request_body = build_google_event_request(
            summary=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            attendee_emails=attendee_emails,
            request_id=request_id,
            internal_booking_id=internal_record_id,
            product_id=product_id,
        )
        event = await self._insert_google_event(access_token, calendar_id, request_body)
        event_id = str(event.get("id") or "")
        meet_url = extract_google_meet_url(event)
        conference_status = google_conference_status(event)
        if event_id and not meet_url:
            event, meet_url, conference_status = await self._wait_for_meet_link(access_token, calendar_id, event_id)
        if not event_id:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google Calendar did not return an event ID")
        return GoogleCalendarEventResult(
            calendar_id=calendar_id,
            event_id=event_id,
            meet_url=meet_url,
            event_url=str(event.get("htmlLink") or ""),
            organizer_user_id=organizer_user_id,
            attendees=[item["email"] for item in request_body["attendees"]],
            conference_status=conference_status,
            sync_status="SYNCED" if meet_url else "MEET_LINK_PENDING",
        )

    async def _insert_google_event(self, access_token: str, calendar_id: str, request_body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.google_api_timeout_seconds) as client:
            response = await client.post(
                f"{GOOGLE_CALENDAR_API_URL}/calendars/{calendar_id}/events",
                params={"conferenceDataVersion": 1, "sendUpdates": "all"},
                json=request_body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            logger.warning("Google event creation failed", extra={"status_code": response.status_code})
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google Calendar event creation failed")
        return response.json()

    async def _wait_for_meet_link(self, access_token: str, calendar_id: str, event_id: str) -> tuple[dict[str, Any], str, str]:
        event: dict[str, Any] = {}
        meet_url = ""
        conference_status = ""
        attempts = max(1, int(settings.google_meet_link_retry_attempts))
        delay = max(0.1, float(settings.google_meet_link_retry_delay_seconds))
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(delay * (2 ** (attempt - 1)))
            async with httpx.AsyncClient(timeout=settings.google_api_timeout_seconds) as client:
                response = await client.get(
                    f"{GOOGLE_CALENDAR_API_URL}/calendars/{calendar_id}/events/{event_id}",
                    params={"conferenceDataVersion": 1},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if response.status_code >= 400:
                logger.warning("Google event fetch failed", extra={"event_id": event_id, "status_code": response.status_code})
                break
            event = response.json()
            meet_url = extract_google_meet_url(event)
            conference_status = google_conference_status(event)
            if meet_url:
                break
        return event, meet_url, conference_status

    async def delete_event(self, organizer_user_id: str, calendar_id: str, event_id: str) -> None:
        if not event_id:
            return
        access_token, _connection = await self._valid_access_token(organizer_user_id)
        async with httpx.AsyncClient(timeout=settings.google_api_timeout_seconds) as client:
            response = await client.delete(
                f"{GOOGLE_CALENDAR_API_URL}/calendars/{calendar_id or 'primary'}/events/{event_id}",
                params={"sendUpdates": "all"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code in {204, 404, 410}:
            return
        logger.warning("Google event cancellation failed", extra={"event_id": event_id, "status_code": response.status_code})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google Calendar cancellation failed")


google_calendar_service = GoogleCalendarService()
