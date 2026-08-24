import unittest
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import settings
from app.core.products import (
    can_create_product,
    normalize_product_name,
    permissions_for_membership,
    validate_organization_email,
)
from app.routers.auth import EMAIL_ALREADY_REGISTERED_MESSAGE, normalize_auth_email, raise_email_already_registered, user_is_registered_and_verified
from app.schemas import ClientBookingCreatePublic, MeetingCreate
from app.services.email import BookingConfirmationMessage, BookingNotificationMessage, InvitationEmailMessage, email_service
from app.services.product_availability import divide_coverage_minutes, local_window_to_utc, window_duration_minutes
from app.services.scheduling import build_slots, default_availability


class ProductSecurityTests(unittest.TestCase):
    def test_normalize_product_name_for_duplicate_detection(self) -> None:
        self.assertEqual(normalize_product_name("  Roadmap   Portal  "), "roadmap portal")

    def test_validate_organization_email_accepts_configured_domain(self) -> None:
        original = settings.organization_email_domain
        settings.organization_email_domain = "evofront.com"
        try:
            self.assertEqual(validate_organization_email(" Member@EvoFront.com "), "member@evofront.com")
        finally:
            settings.organization_email_domain = original

    def test_validate_organization_email_rejects_personal_domain(self) -> None:
        original = settings.organization_email_domain
        settings.organization_email_domain = "evofront.com"
        try:
            with self.assertRaises(HTTPException) as raised:
                validate_organization_email("member@gmail.com")
            self.assertEqual(raised.exception.status_code, 422)
        finally:
            settings.organization_email_domain = original

    def test_calendar_controller_membership_has_invitation_permissions(self) -> None:
        permissions = permissions_for_membership({"role": "calendar_controller"})
        self.assertIn("create_meetings", permissions)
        self.assertIn("invite_members", permissions)
        self.assertIn("manage_members", permissions)

    def test_member_role_cannot_manage_members(self) -> None:
        permissions = permissions_for_membership({"role": "member"})
        self.assertNotIn("manage_members", permissions)

    def test_global_calendar_controller_can_create_products(self) -> None:
        self.assertTrue(can_create_product({"role": "calendar_controller"}))


class EmailOtpRegistrationTests(unittest.TestCase):
    def test_auth_email_normalization_trims_and_lowercases(self) -> None:
        self.assertEqual(normalize_auth_email("  Member@EvoFront.COM  "), "member@evofront.com")

    def test_verified_password_user_is_registered(self) -> None:
        self.assertTrue(
            user_is_registered_and_verified(
                {"email_verified": True, "password_hash": "hashed", "auth_provider": "password"}
            )
        )

    def test_unverified_existing_user_can_continue_verification(self) -> None:
        self.assertFalse(
            user_is_registered_and_verified(
                {"email_verified": False, "password_hash": "hashed", "auth_provider": "password"}
            )
        )

    def test_invited_user_without_password_does_not_block_registration(self) -> None:
        self.assertFalse(
            user_is_registered_and_verified(
                {"email_verified": True, "password_hash": "", "auth_provider": "invited"}
            )
        )

    def test_already_registered_error_uses_actionable_code(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            raise_email_already_registered()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "EMAIL_ALREADY_REGISTERED")
        self.assertEqual(raised.exception.detail["message"], EMAIL_ALREADY_REGISTERED_MESSAGE)
        self.assertEqual(raised.exception.detail["nextAction"], "LOGIN")


class MeetingValidationTests(unittest.TestCase):
    def test_meeting_requires_recipients_when_not_inviting_team(self) -> None:
        start = datetime.now(UTC) + timedelta(hours=1)
        with self.assertRaises(ValidationError):
            MeetingCreate(
                title="Planning",
                start_time=start,
                end_time=start + timedelta(minutes=30),
                timezone="UTC",
                recipient_user_ids=[],
                invite_entire_team=False,
            )


class PublicBookingValidationTests(unittest.TestCase):
    def test_public_booking_payload_accepts_interview_fields(self) -> None:
        payload = ClientBookingCreatePublic(
            slot_key="x" * 32,
            client_name="Mukesh",
            client_email="mukesh.g@evofront.com",
            client_phone="+91 98765 43210",
            client_company="Evofront",
            product_reference_number="ACC-123",
            issue_category="Interview",
            issue_title="Schedule interview",
            issue_description="Discuss the product implementation.",
            client_timezone="Asia/Kolkata",
            consent_confirmed=True,
        )
        self.assertEqual(payload.client_phone, "+91 98765 43210")
        self.assertEqual(payload.product_reference_number, "ACC-123")

    def test_public_booking_payload_rejects_invalid_phone(self) -> None:
        with self.assertRaises(ValidationError):
            ClientBookingCreatePublic(
                slot_key="x" * 32,
                client_name="Mukesh",
                client_email="mukesh.g@evofront.com",
                client_phone="<script>",
                issue_category="Interview",
                issue_title="Schedule interview",
                issue_description="Discuss the product implementation.",
                client_timezone="Asia/Kolkata",
                consent_confirmed=True,
            )


class EmailServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_email_provider_records_pending_integration(self) -> None:
        original = settings.email_enabled
        settings.email_enabled = False
        try:
            result = await email_service.send_meeting_invitation(
                InvitationEmailMessage(
                    recipient_email="member@evofront.com",
                    recipient_name="Member",
                    organizer_name="Controller",
                    product_name="Calendar",
                    title="Planning",
                    description="",
                    start_time=datetime.now(UTC) + timedelta(hours=1),
                    end_time=datetime.now(UTC) + timedelta(hours=2),
                    timezone="UTC",
                    location="",
                    meeting_url="",
                    invitation_link="https://app.example.test/invite/token",
                )
            )
            self.assertEqual(result.status, "PENDING_EMAIL_INTEGRATION")
        finally:
            settings.email_enabled = original

    async def test_disabled_booking_email_provider_records_pending_integration(self) -> None:
        original = settings.email_enabled
        settings.email_enabled = False
        try:
            result = await email_service.send_booking_notification(
                BookingNotificationMessage(
                    recipient_email="member@evofront.com",
                    recipient_name="Member",
                    product_name="Support Product",
                    client_name="Client",
                    client_company="Client Co",
                    issue_category="Technical",
                    issue_title="API issue",
                    issue_description="Need help",
                    priority="normal",
                    start_time=datetime.now(UTC) + timedelta(hours=1),
                    end_time=datetime.now(UTC) + timedelta(hours=2),
                    timezone="UTC",
                    duration_minutes=60,
                    booking_link="https://app.example.test/support/confirmation/token",
                )
            )
            self.assertEqual(result.status, "PENDING_EMAIL_INTEGRATION")
        finally:
            settings.email_enabled = original

    async def test_disabled_booking_confirmation_provider_records_pending_integration(self) -> None:
        original = settings.email_enabled
        settings.email_enabled = False
        try:
            result = await email_service.send_booking_confirmation(
                BookingConfirmationMessage(
                    recipient_email="client@example.com",
                    recipient_name="Client",
                    product_name="Support Product",
                    event_title="Support booking",
                    organizer_name="Support Team",
                    start_time=datetime.now(UTC) + timedelta(hours=1),
                    end_time=datetime.now(UTC) + timedelta(hours=2),
                    timezone="UTC",
                    location="",
                    meeting_url="https://meet.example.test/room",
                    confirmation_link="https://app.example.test/book/demo",
                )
            )
            self.assertEqual(result.status, "PENDING_EMAIL_INTEGRATION")
        finally:
            settings.email_enabled = original


class ProductAvailabilityDistributionTests(unittest.TestCase):
    def test_one_member_gets_full_shared_support_window(self) -> None:
        self.assertEqual(divide_coverage_minutes("09:00", "17:00", 1), [("09:00", "17:00")])

    def test_three_members_split_480_minutes_without_gaps(self) -> None:
        self.assertEqual(
            divide_coverage_minutes("09:00", "17:00", 3),
            [("09:00", "11:40"), ("11:40", "14:20"), ("14:20", "17:00")],
        )

    def test_full_day_window_splits_1440_minutes_into_shifts(self) -> None:
        self.assertEqual(window_duration_minutes("00:00", "00:00"), 1440)
        self.assertEqual(
            divide_coverage_minutes("00:00", "00:00", 3),
            [("00:00", "08:00"), ("08:00", "16:00"), ("16:00", "00:00")],
        )

    def test_overnight_window_splits_across_midnight(self) -> None:
        self.assertEqual(window_duration_minutes("22:00", "06:00"), 480)
        self.assertEqual(
            divide_coverage_minutes("22:00", "06:00", 2),
            [("22:00", "02:00"), ("02:00", "06:00")],
        )

    def test_local_window_to_utc_extends_overnight_end_to_next_day(self) -> None:
        start, end = local_window_to_utc(datetime(2026, 8, 23).date(), "22:00", "06:00", "UTC")
        self.assertEqual((end - start).total_seconds() // 3600, 8)

    def test_remainder_minutes_are_distributed_deterministically(self) -> None:
        self.assertEqual(
            divide_coverage_minutes("09:00", "17:00", 7),
            [
                ("09:00", "10:09"),
                ("10:09", "11:18"),
                ("11:18", "12:27"),
                ("12:27", "13:36"),
                ("13:36", "14:44"),
                ("14:44", "15:52"),
                ("15:52", "17:00"),
            ],
        )

    def test_no_members_returns_no_coverage(self) -> None:
        self.assertEqual(divide_coverage_minutes("09:00", "17:00", 0), [])

    def test_default_legacy_availability_is_24_7(self) -> None:
        availability = default_availability("UTC")
        self.assertEqual(len(availability["windows"]), 7)
        self.assertTrue(all(window["enabled"] for window in availability["windows"]))
        self.assertTrue(all(window["start"] == "00:00" and window["end"] == "00:00" for window in availability["windows"]))

    def test_legacy_full_day_window_generates_all_day_slots(self) -> None:
        target_date = (datetime.now(UTC) + timedelta(days=5)).date()
        availability = {
            "timezone": "UTC",
            "windows": [{"day": target_date.weekday(), "start": "00:00", "end": "00:00", "enabled": True}],
            "min_notice_minutes": 0,
            "slot_interval_minutes": 60,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 0,
        }
        slots = build_slots(availability, {"duration_minutes": 60}, [], target_date, target_date)
        self.assertEqual(len(slots), 24)
        self.assertEqual(slots[0]["local_time"], "00:00")
        self.assertEqual(slots[-1]["local_time"], "23:00")

    def test_legacy_overnight_window_keeps_slots_on_selected_date(self) -> None:
        target_date = (datetime.now(UTC) + timedelta(days=5)).date()
        previous_date = target_date - timedelta(days=1)
        availability = {
            "timezone": "UTC",
            "windows": [
                {"day": previous_date.weekday(), "start": "22:00", "end": "02:00", "enabled": True},
                {"day": target_date.weekday(), "start": "22:00", "end": "02:00", "enabled": True},
            ],
            "min_notice_minutes": 0,
            "slot_interval_minutes": 60,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 0,
        }
        slots = build_slots(availability, {"duration_minutes": 60}, [], target_date, target_date)
        self.assertEqual([slot["local_time"] for slot in slots], ["00:00", "01:00", "22:00", "23:00"])
        self.assertTrue(all(slot["local_date"] == target_date for slot in slots))
