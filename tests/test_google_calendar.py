import unittest
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from app.core.config import settings
from app.services.google_calendar import (
    build_google_event_request,
    event_time_payload,
    extract_google_meet_url,
    google_calendar_service,
    google_conference_status,
    hash_google_oauth_state,
)


class GoogleCalendarHelperTests(unittest.TestCase):
    def test_oauth_state_hash_is_stable_and_not_raw_state(self) -> None:
        state = "state-value"
        self.assertEqual(hash_google_oauth_state(state), hash_google_oauth_state(state))
        self.assertNotEqual(hash_google_oauth_state(state), state)

    def test_extract_meet_link_prefers_hangout_link(self) -> None:
        event = {
            "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
            "conferenceData": {
                "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/other"}]
            },
        }
        self.assertEqual(extract_google_meet_url(event), "https://meet.google.com/aaa-bbbb-ccc")

    def test_extract_meet_link_from_video_entry_point(self) -> None:
        event = {
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "phone", "uri": "tel:+100000000"},
                    {"entryPointType": "video", "uri": "https://meet.google.com/ddd-eeee-fff"},
                ]
            }
        }
        self.assertEqual(extract_google_meet_url(event), "https://meet.google.com/ddd-eeee-fff")

    def test_conference_status_reads_google_status_code(self) -> None:
        event = {"conferenceData": {"createRequest": {"status": {"statusCode": "pending"}}}}
        self.assertEqual(google_conference_status(event), "pending")

    def test_event_time_payload_preserves_iana_timezone(self) -> None:
        payload = event_time_payload(datetime(2026, 8, 25, 4, 30, tzinfo=UTC), "Asia/Kolkata")
        self.assertEqual(payload["timeZone"], "Asia/Kolkata")
        self.assertTrue(payload["dateTime"].startswith("2026-08-25T10:00:00"))

    def test_build_event_request_includes_conference_and_attendees(self) -> None:
        start = datetime.now(UTC) + timedelta(days=1)
        body = build_google_event_request(
            summary="Support",
            description="Need help",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="UTC",
            attendee_emails=["client@example.com", "Client@Example.com", ""],
            request_id="booking-123",
            internal_booking_id="internal-id",
            product_id="product-id",
        )
        self.assertEqual(body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"], "hangoutsMeet")
        self.assertEqual(body["conferenceData"]["createRequest"]["requestId"], "booking-123")
        self.assertEqual(body["attendees"], [{"email": "client@example.com"}])
        self.assertEqual(body["extendedProperties"]["private"]["calendarBookingId"], "internal-id")


class GoogleCalendarEncryptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            "google_calendar_enabled": settings.google_calendar_enabled,
            "google_client_id": settings.google_client_id,
            "google_client_secret": settings.google_client_secret,
            "google_redirect_uri": settings.google_redirect_uri,
            "google_token_encryption_key": settings.google_token_encryption_key,
        }
        settings.google_calendar_enabled = True
        settings.google_client_id = "client-id"
        settings.google_client_secret = "client-secret"
        settings.google_redirect_uri = "http://localhost:8001/api/integrations/google/callback"
        settings.google_token_encryption_key = Fernet.generate_key().decode("utf-8")

    def tearDown(self) -> None:
        for key, value in self.original.items():
            setattr(settings, key, value)

    def test_tokens_encrypt_and_decrypt_without_plaintext_storage(self) -> None:
        encrypted = google_calendar_service.encrypt_token("refresh-token")
        self.assertNotIn("refresh-token", encrypted)
        self.assertEqual(google_calendar_service.decrypt_token(encrypted), "refresh-token")
