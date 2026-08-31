import unittest
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import settings
from app.core.products import (
    can_create_product,
    normalize_product_name,
    normalize_workspace_domain,
    normalize_workspace_domains,
    permissions_for_membership,
    validate_organization_email,
)
from app.core.widget import approved_widget_origins, is_local_development_origin
from app.routers.auth import (
    EMAIL_ALREADY_REGISTERED_MESSAGE,
    EMAIL_NOT_REGISTERED_MESSAGE,
    PASSWORD_INCORRECT_MESSAGE,
    normalize_auth_email,
    raise_email_already_registered,
    user_is_registered_and_verified,
)
from app.schemas import ClientBookingCreatePublic, ClientBookingOut, MeetingCreate
from app.services.email import (
    BookingConfirmationMessage,
    BookingNotificationMessage,
    InvitationEmailMessage,
    SendGridInvitationProvider,
    email_service,
)
from app.services.booking_claims import CLAIM_TOKEN_MAX_DAYS, _ensure_claim_token_live
from app.services.product_availability import divide_coverage_minutes, local_window_to_utc, window_duration_minutes
from app.services.product_members import (
    MEMBER_VERIFY_DAYS,
    has_login_identity,
    is_verified,
    new_verification_payload,
    verification_reason,
    verification_state,
)
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
        self.assertNotIn("manage_controllers", permissions)

    def test_product_owner_can_manage_controllers(self) -> None:
        permissions = permissions_for_membership({"role": "product_owner"})
        self.assertIn("manage_controllers", permissions)

    def test_member_role_cannot_manage_members(self) -> None:
        permissions = permissions_for_membership({"role": "member"})
        self.assertNotIn("manage_members", permissions)

    def test_global_calendar_controller_can_create_products(self) -> None:
        self.assertTrue(can_create_product({"role": "calendar_controller"}))

    def test_workspace_domain_normalization_uses_exact_origin(self) -> None:
        self.assertEqual(normalize_workspace_domain("WWW.Example.com/path"), "https://www.example.com")
        self.assertEqual(normalize_workspace_domain("http://localhost:3000/demo"), "http://localhost:3000")

    def test_workspace_domain_list_deduplicates_origins(self) -> None:
        self.assertEqual(
            normalize_workspace_domains(["https://example.com/a", "https://example.com/b", "http://localhost:3000"]),
            ["https://example.com", "http://localhost:3000"],
        )

    def test_widget_approved_origins_do_not_use_suffix_matching(self) -> None:
        origins = approved_widget_origins({"approved_domains": ["https://websitex.com"]})
        self.assertIn("https://websitex.com", origins)
        self.assertNotIn("https://evil-websitex.com", origins)

    def test_localhost_is_only_development_origin_shape(self) -> None:
        self.assertTrue(is_local_development_origin("http://localhost:3000"))
        self.assertFalse(is_local_development_origin("https://example.com"))


class MemberVerificationTests(unittest.TestCase):
    def test_pending_membership_is_not_rotation_eligible(self) -> None:
        membership = {"member_verification_status": "pending", "role": "member", "status": "active"}
        user = {"status": "invited", "password_hash": "", "auth_provider": "invited"}
        self.assertEqual(verification_state(membership, user), "pending")
        self.assertFalse(is_verified(membership, user))

    def test_verified_membership_without_login_is_rotation_eligible(self) -> None:
        membership = {"member_verification_status": "verified", "role": "member", "status": "active"}
        user = {"status": "invited", "password_hash": "", "auth_provider": "invited"}
        self.assertTrue(is_verified(membership, user))
        self.assertFalse(has_login_identity(user))

    def test_expired_verification_window_reports_expired(self) -> None:
        membership = {
            "member_verification_status": "pending",
            "member_verification_expires_at": datetime.now(UTC) - timedelta(days=1),
        }
        self.assertEqual(verification_state(membership, {"status": "invited"}), "expired")
        self.assertEqual(verification_reason("expired"), "Verification link expired")

    def test_legacy_membership_with_active_login_stays_verified(self) -> None:
        membership = {"role": "calendar_controller", "status": "active"}
        user = {"status": "active", "password_hash": "hashed", "auth_provider": "password"}
        self.assertTrue(is_verified(membership, user))
        self.assertTrue(has_login_identity(user))

    def test_verification_payload_expires_in_seven_days(self) -> None:
        payload = new_verification_payload("owner-id")
        self.assertEqual(payload["member_verification_status"], "pending")
        self.assertTrue(payload["member_verification_token"])
        remaining_days = (payload["member_verification_expires_at"] - datetime.now(UTC)).days
        self.assertEqual(remaining_days, MEMBER_VERIFY_DAYS - 1)


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

    def test_login_error_messages_are_specific(self) -> None:
        self.assertEqual(EMAIL_NOT_REGISTERED_MESSAGE, "This email is not registered. Please sign up first.")
        self.assertEqual(PASSWORD_INCORRECT_MESSAGE, "Password is incorrect.")


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

    def test_controller_request_email_includes_website_origin(self) -> None:
        start = datetime.now(UTC) + timedelta(hours=1)
        message = BookingNotificationMessage(
            recipient_email="controller@amazon.com",
            recipient_name="AWS Workspace",
            product_name="Amazon AWS",
            client_name="Client",
            client_company="Acme",
            issue_category="Team connection",
            issue_title="Connect with AWS team",
            issue_description="Need a meeting",
            priority="normal",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="UTC",
            duration_minutes=30,
            booking_link="https://app.example.test/dashboard",
            booking_status="pending_approval",
            source_domain="https://aws.amazon.com",
        )
        text = SendGridInvitationProvider.booking_plain_text(message, "Monday")
        html_body = SendGridInvitationProvider.booking_html(message, "Monday")
        self.assertIn("booking request", text)
        self.assertIn("Website origin: https://aws.amazon.com", text)
        self.assertIn("https://aws.amazon.com", html_body)

    def test_pending_client_confirmation_uses_request_received_copy(self) -> None:
        start = datetime.now(UTC) + timedelta(hours=1)
        message = BookingConfirmationMessage(
            recipient_email="client@example.com",
            recipient_name="Client",
            product_name="Amazon AWS",
            event_title="Amazon AWS team connection request",
            organizer_name="Amazon AWS Support Team",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="UTC",
            location="Pending controller approval",
            meeting_url="",
            confirmation_link="https://app.example.test/support/token",
            pending_approval=True,
        )
        text = SendGridInvitationProvider.booking_confirmation_plain_text(message, "Monday")
        self.assertIn("request was received", text)
        self.assertNotIn("is confirmed", text)


class WorkspaceBookingSchemaTests(unittest.TestCase):
    def test_client_booking_out_exposes_source_domain(self) -> None:
        start = datetime.now(UTC) + timedelta(hours=1)
        booking = ClientBookingOut(
            id="abc123abc123abc123abc123",
            organization_id="default",
            product_id="prod123prod123prod123prod",
            assigned_member_id="member123member12",
            client_name="Client",
            client_email="client@example.com",
            issue_category="Team connection",
            issue_title="Connect",
            priority="normal",
            start_time_utc=start,
            end_time_utc=start + timedelta(minutes=30),
            client_timezone="UTC",
            product_timezone="UTC",
            status="pending_approval",
            assignment_strategy="controller_review",
            public_booking_reference="ref123",
            source_domain="https://aws.amazon.com",
            widget_id="widget-token",
            booking_mode="approval",
            created_at=start,
            updated_at=start,
        )
        self.assertEqual(booking.source_domain, "https://aws.amazon.com")
        self.assertEqual(booking.widget_id, "widget-token")
        self.assertEqual(booking.booking_mode, "approval")


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

class BookingClaimTokenTests(unittest.TestCase):
    def test_expired_claim_token_is_rejected(self):
        alert = {"claim_expires_at": datetime.now(UTC) - timedelta(minutes=1)}
        with self.assertRaises(HTTPException) as caught:
            _ensure_claim_token_live(alert)
        self.assertEqual(caught.exception.status_code, 410)

    def test_live_claim_token_is_accepted(self):
        alert = {"claim_expires_at": datetime.now(UTC) + timedelta(hours=2)}
        self.assertIsNone(_ensure_claim_token_live(alert))

    def test_legacy_alert_without_expiry_is_accepted(self):
        self.assertIsNone(_ensure_claim_token_live({}))
        self.assertIsNone(_ensure_claim_token_live({"claim_expires_at": None}))

    def test_claim_window_is_capped(self):
        self.assertEqual(CLAIM_TOKEN_MAX_DAYS, 14)
