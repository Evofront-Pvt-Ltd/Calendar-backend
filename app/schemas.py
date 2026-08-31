from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class WeeklyWindow(BaseModel):
    day: int = Field(ge=0, le=6, description="0 is Monday and 6 is Sunday")
    start: str = Field(pattern=TIME_PATTERN)
    end: str = Field(pattern=TIME_PATTERN)
    enabled: bool = True

    @field_validator("end")
    @classmethod
    def end_after_start(cls, value: str, info):
        # Equal start/end represents a full-day window; an earlier end time represents an overnight window.
        return value


class AvailabilityIn(BaseModel):
    timezone: str = Field(min_length=2, max_length=80)
    windows: list[WeeklyWindow] = Field(min_length=1)
    min_notice_minutes: int = Field(default=60, ge=0, le=60 * 24 * 30)
    slot_interval_minutes: int = Field(default=30, ge=5, le=240)
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=0, ge=0, le=240)


class ProductAvailabilityPolicyIn(BaseModel):
    support_start_time: str = Field(default="00:00", pattern=TIME_PATTERN)
    support_end_time: str = Field(default="00:00", pattern=TIME_PATTERN)
    timezone: str = Field(default="Asia/Kolkata", min_length=2, max_length=80)
    distribution_mode: Literal[
        "equal_sequential",
        "manual",
        "rotating_daily",
        "round_robin",
        "overlapping",
    ] = "equal_sequential"
    appointment_duration_minutes: int = Field(default=30, ge=5, le=480)
    slot_interval_minutes: int = Field(default=30, ge=5, le=240)
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=0, ge=0, le=240)
    minimum_booking_notice_minutes: int = Field(default=60, ge=0, le=60 * 24 * 30)
    maximum_advance_booking_days: int = Field(default=30, ge=1, le=365)
    maximum_concurrent_bookings: int = Field(default=1, ge=1, le=20)
    active: bool = True

    @model_validator(mode="after")
    def support_end_after_start(self):
        # Equal start/end represents 24-hour coverage; an earlier end time represents an overnight support window.
        return self


class ProductAvailabilityPolicyOut(ProductAvailabilityPolicyIn):
    id: str
    organization_id: str
    product_id: str
    created_by: str = ""
    updated_by: str = ""
    created_at: datetime
    updated_at: datetime


class MemberAvailabilityUpsert(BaseModel):
    member_id: str
    date: date
    start_time: str = Field(pattern=TIME_PATTERN)
    end_time: str = Field(pattern=TIME_PATTERN)
    timezone: str = Field(default="Asia/Kolkata", min_length=2, max_length=80)
    source: Literal["GENERATED", "MANUAL"] = "MANUAL"
    status: Literal["available", "unavailable", "on_leave"] = "available"
    change_reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def member_end_after_start(self):
        # Equal start/end represents a full-day manual shift; an earlier end time represents an overnight shift.
        return self


class MemberAvailabilityOut(BaseModel):
    id: str
    organization_id: str
    product_id: str
    member_id: str
    member_name: str = ""
    member_role: str = ""
    day_of_week: int
    date: date
    start_time: str
    end_time: str
    timezone: str
    recurrence_rule: str = ""
    source: Literal["GENERATED", "MANUAL"]
    status: str
    effective_from: date | None = None
    effective_until: date | None = None
    created_by: str = ""
    updated_by: str = ""
    created_at: datetime
    updated_at: datetime


class AvailabilityExceptionCreate(BaseModel):
    member_id: str = ""
    exception_date: date
    start_time: str = Field(pattern=TIME_PATTERN)
    end_time: str = Field(pattern=TIME_PATTERN)
    type: Literal["break", "leave", "unavailable", "holiday"] = "unavailable"
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def exception_end_after_start(self):
        # Equal start/end represents a full-day exception; an earlier end time represents an overnight exception.
        return self


class AvailabilityExceptionOut(AvailabilityExceptionCreate):
    id: str
    organization_id: str
    product_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class GenerateCoverageRequest(BaseModel):
    date: date
    preserve_manual_overrides: bool = True
    force_regenerate: bool = False


class AvailableSlotOut(BaseModel):
    start_time_utc: datetime
    end_time_utc: datetime
    local_date: date
    local_time: str
    label: str
    slot_key: str
    member_id: str = ""
    member_name: str = ""
    source: str = ""


class ProductMemberAvailabilitySummary(BaseModel):
    member_id: str
    membership_id: str
    full_name: str
    role: str
    status: str
    included_in_rotation: bool
    reason: str = ""


class ClientBookingCreatePublic(BaseModel):
    slot_key: str = Field(min_length=20, max_length=1200)
    client_name: str = Field(min_length=2, max_length=80)
    client_email: EmailStr
    client_phone: str = Field(default="", max_length=32)
    client_company: str = Field(default="", max_length=120)
    product_reference_number: str = Field(default="", max_length=80)
    issue_category: str = Field(default="General", max_length=80)
    issue_title: str = Field(min_length=2, max_length=140)
    issue_description: str = Field(default="", max_length=1200)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    client_timezone: str = Field(default="Asia/Kolkata", min_length=2, max_length=80)
    consent_confirmed: bool = False

    @field_validator("consent_confirmed")
    @classmethod
    def consent_required(cls, value: bool):
        if not value:
            raise ValueError("Consent is required to create a booking")
        return value

    @field_validator("client_phone")
    @classmethod
    def valid_phone(cls, value: str):
        cleaned = value.strip()
        if not cleaned:
            return ""
        allowed = set("+()- 0123456789")
        if any(char not in allowed for char in cleaned) or len(cleaned) < 7:
            raise ValueError("Enter a valid phone number")
        return cleaned


class ClientBookingOut(BaseModel):
    id: str
    organization_id: str
    product_id: str
    assigned_member_id: str
    assigned_member_name: str = ""
    client_name: str
    client_email: EmailStr
    client_phone: str = ""
    client_company: str = ""
    product_reference_number: str = ""
    issue_category: str
    issue_title: str
    issue_description: str = ""
    priority: str
    start_time_utc: datetime
    end_time_utc: datetime
    client_timezone: str
    product_timezone: str
    status: Literal["pending_approval", "scheduled", "cancelled", "rescheduled", "rejected"] = "scheduled"
    assignment_strategy: str
    assignment_reason: str = ""
    public_booking_reference: str
    confirmation_link: str = ""
    source_domain: str = ""
    widget_id: str = ""
    booking_mode: str = ""
    google_meet_url: str = ""
    google_event_url: str = ""
    google_sync_status: str = "DISABLED"
    google_conference_status: str = ""
    created_at: datetime
    updated_at: datetime


class BookingNotificationOut(BaseModel):
    id: str
    organization_id: str
    product_id: str
    booking_id: str
    recipient_user_id: str = ""
    recipient_email: EmailStr | None = None
    channel: Literal["in_app", "email", "sms", "push", "calendar"]
    type: str
    status: str
    provider: str = ""
    provider_message_id: str = ""
    attempts: int = 0
    last_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_at: datetime | None = None
    failure_reason: str = ""
    idempotency_key: str
    created_at: datetime
    updated_at: datetime


class BookingAssignmentHistoryOut(BaseModel):
    id: str
    booking_id: str
    organization_id: str
    product_id: str
    previous_product_id: str = ""
    new_product_id: str = ""
    previous_team_id: str = ""
    new_team_id: str = ""
    previous_member_id: str = ""
    new_member_id: str = ""
    changed_by: str
    reason: str = ""
    changed_at: datetime


class GoogleCalendarConnectOut(BaseModel):
    authorization_url: str


class GoogleCalendarStatusOut(BaseModel):
    enabled: bool
    configured: bool
    connected: bool
    connection_status: str
    provider_email: str = ""
    calendar_id: str = "primary"
    granted_scopes: list[str] = Field(default_factory=list)
    token_expiry: datetime | None = None
    last_sync_at: datetime | None = None
    last_error_code: str = ""
    last_error_message: str = ""


class AvailabilityAuditLogOut(BaseModel):
    id: str
    organization_id: str
    product_id: str
    member_id: str = ""
    action: str
    previous_value: dict = Field(default_factory=dict)
    new_value: dict = Field(default_factory=dict)
    changed_by: str
    change_reason: str = ""
    created_at: datetime


class TeamAvailabilityOut(BaseModel):
    product_id: str
    product_name: str
    date: date
    timezone: str
    policy: ProductAvailabilityPolicyOut
    members: list[ProductMemberAvailabilitySummary] = Field(default_factory=list)
    coverage: list[MemberAvailabilityOut] = Field(default_factory=list)
    available_slots: list[AvailableSlotOut] = Field(default_factory=list)
    bookings: list[ClientBookingOut] = Field(default_factory=list)
    notifications: list[BookingNotificationOut] = Field(default_factory=list)
    assignment_history: list[BookingAssignmentHistoryOut] = Field(default_factory=list)
    exceptions: list[AvailabilityExceptionOut] = Field(default_factory=list)


class PublicProductBookingOut(BaseModel):
    product_name: str
    description: str = ""
    timezone: str
    support_start_time: str
    support_end_time: str
    appointment_duration_minutes: int
    email_enabled: bool = False


class PublicLandingProductOut(BaseModel):
    name: str
    description: str = ""
    icon: str = ""
    color: str = "#006bff"
    booking_token: str
    timezone: str
    support_start_time: str
    support_end_time: str
    appointment_duration_minutes: int
    booking_mode: str = "instant"
    widget_button_label: str = "Book Now"
    widget_action_label: str = "Schedule to connect team"


class WidgetConfigOut(BaseModel):
    workspace_name: str
    public_widget_id: str
    enabled: bool
    button_label: str = "Book Now"
    action_label: str = "Schedule to connect team"
    position: Literal["right", "left"] = "right"
    primary_color: str = "#006bff"
    booking_mode: str = "instant"
    timezone: str = "Asia/Kolkata"
    product: PublicLandingProductOut


class UserPublic(BaseModel):
    id: str
    name: str
    email: EmailStr
    email_verified: bool = True
    email_verified_at: datetime | None = None
    auth_provider: str = "password"
    profile_image: str = ""
    role: str = "calendar_controller"
    organization_id: str = "default"
    status: str = "active"
    slug: str
    timezone: str
    availability: AvailabilityIn | None = None
    created_at: datetime
    updated_at: datetime


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    timezone: str = Field(default="Asia/Kolkata", min_length=2, max_length=80)


class RegisterStartResponse(BaseModel):
    email: EmailStr
    expires_in_minutes: int
    resend_available_in_seconds: int
    delivery_provider: Literal["console", "sendgrid"] = "console"
    message: str


class RegisterResendRequest(BaseModel):
    email: EmailStr


class RegisterVerifyRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPublic


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=512)
    all_sessions: bool = False


class LogoutResponse(BaseModel):
    success: bool = True
    message: str = "Signed out"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    # Deliberately identical whether or not the address has an account, so the endpoint
    # cannot be used to discover who is registered.
    success: bool = True
    expires_in_minutes: int
    message: str


class PasswordResetTokenCheck(BaseModel):
    valid: bool
    email: str = ""
    expires_in_minutes: int = 0
    message: str


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=8, max_length=128)


class ResetPasswordResponse(BaseModel):
    success: bool = True
    message: str = "Password updated. Please sign in with your new password."


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=600)
    icon: str = Field(default="", max_length=10)
    color: str = Field(default="#006bff", pattern=r"^#[0-9a-fA-F]{6}$")
    status: Literal["active", "inactive"] = "active"
    approved_domains: list[str] = Field(default_factory=list, max_length=25)
    controller_email: str = Field(default="", max_length=254)
    support_email: str = Field(default="", max_length=254)
    booking_mode: Literal["instant", "approval"] = "instant"
    widget_enabled: bool = True
    widget_button_label: str = Field(default="Book Now", max_length=40)
    widget_action_label: str = Field(default="Schedule to connect team", max_length=80)
    widget_position: Literal["right", "left"] = "right"


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=600)
    icon: str | None = Field(default=None, max_length=10)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    status: Literal["active", "inactive"] | None = None
    approved_domains: list[str] | None = Field(default=None, max_length=25)
    controller_email: str | None = Field(default=None, max_length=254)
    support_email: str | None = Field(default=None, max_length=254)
    booking_mode: Literal["instant", "approval"] | None = None
    widget_enabled: bool | None = None
    widget_button_label: str | None = Field(default=None, max_length=40)
    widget_action_label: str | None = Field(default=None, max_length=80)
    widget_position: Literal["right", "left"] | None = None


class ProductOut(BaseModel):
    id: str
    organization_id: str
    name: str
    description: str = ""
    icon: str = ""
    color: str = "#006bff"
    status: Literal["active", "inactive"] = "active"
    created_by: str
    membership_role: str
    permissions: list[str] = Field(default_factory=list)
    can_create_product: bool = False
    member_count: int = 0
    public_booking_token: str = ""
    public_booking_path: str = ""
    approved_domains: list[str] = Field(default_factory=list)
    controller_email: str = ""
    support_email: str = ""
    booking_mode: Literal["instant", "approval"] = "instant"
    widget_enabled: bool = True
    widget_button_label: str = "Book Now"
    widget_action_label: str = "Schedule to connect team"
    widget_position: Literal["right", "left"] = "right"
    created_at: datetime
    updated_at: datetime


class ProductControllerCreate(BaseModel):
    email: EmailStr


class ProductControllerOut(BaseModel):
    id: str
    product_id: str
    email: EmailStr
    status: Literal["pending", "verified", "expired", "revoked"]
    added_by: str = ""
    verified_at: datetime | None = None
    verification_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ControllerVerifyOut(BaseModel):
    status: str
    email: str
    product_id: str
    message: str


class MemberVerifyOut(BaseModel):
    status: str
    email: str
    product_name: str = ""
    has_login: bool = False
    message: str


class BookingClaimAlertOut(BaseModel):
    id: str
    product_id: str
    booking_id: str
    status: str
    audience: str = ""
    claim_token: str = ""
    created_at: datetime | None = None
    client_name: str = ""
    client_email: str = ""
    client_company: str = ""
    issue_title: str = ""
    issue_category: str = ""
    priority: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None
    timezone: str = ""
    issue_description: str = ""


class PublicBookingClaimOut(BaseModel):
    token: str
    status: str
    booking_status: str
    product_name: str
    client_name: str
    issue_title: str
    issue_category: str
    priority: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    timezone: str = ""
    can_accept: bool = False


class BookingAssignmentUpdate(BaseModel):
    member_id: str = Field(min_length=12, max_length=64)
    reason: str = Field(default="", max_length=300)


class BookingDecisionRequest(BaseModel):
    reason: str = Field(default="", max_length=300)


class ProductMemberCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    role: Literal["calendar_controller", "member", "viewer"] = "member"
    status: Literal["active", "inactive"] = "active"


class ProductMemberUpdate(BaseModel):
    role: Literal["calendar_controller", "member", "viewer"] | None = None
    status: Literal["active", "inactive"] | None = None


class ProductMemberOut(BaseModel):
    id: str
    product_id: str
    user_id: str
    full_name: str
    email: EmailStr
    role: str
    membership_status: str
    invitation_status: str = "pending_email_integration"
    verification_status: Literal["pending", "verified", "expired"] = "pending"
    verified_at: datetime | None = None
    verification_expires_at: datetime | None = None
    has_login: bool = False
    added_by: str
    added_by_name: str = ""
    date_added: datetime
    joined_at: datetime | None = None
    last_invitation_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MeetingInvitationOut(BaseModel):
    id: str
    meeting_id: str
    product_id: str
    recipient_user_id: str
    recipient_name: str = ""
    recipient_email: EmailStr
    invitation_status: str
    email_delivery_status: str
    provider_message_id: str = ""
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_at: datetime | None = None
    failure_reason: str = ""
    invitation_link: str
    created_at: datetime
    updated_at: datetime


class MeetingCreate(BaseModel):
    title: str = Field(min_length=2, max_length=140)
    description: str = Field(default="", max_length=1200)
    start_time: datetime
    end_time: datetime
    timezone: str = Field(default="Asia/Kolkata", min_length=2, max_length=80)
    location: str = Field(default="", max_length=200)
    meeting_url: str = Field(default="", max_length=500)
    recipient_user_ids: list[str] = Field(default_factory=list, max_length=250)
    invite_entire_team: bool = False

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        if not self.invite_entire_team and not self.recipient_user_ids:
            raise ValueError("Select at least one team member")
        return self


class MeetingOut(BaseModel):
    id: str
    product_id: str
    organizer_id: str
    title: str
    description: str = ""
    start_time: datetime
    end_time: datetime
    timezone: str
    location: str = ""
    meeting_url: str = ""
    status: Literal["scheduled", "cancelled"] = "scheduled"
    invitation_count: int = 0
    pending_email_count: int = 0
    invitations: list[MeetingInvitationOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PublicMeetingInvitationOut(BaseModel):
    product_name: str
    meeting_title: str
    description: str = ""
    start_time: datetime
    end_time: datetime
    timezone: str
    location: str = ""
    meeting_url: str = ""
    recipient_email: EmailStr
    invitation_status: str
    email_delivery_status: str


class EventQuestion(BaseModel):
    label: str = Field(min_length=2, max_length=120)
    required: bool = False


class EventTypeCreate(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=600)
    duration_minutes: int = Field(default=30, ge=5, le=480)
    location_type: Literal["phone", "video", "in_person", "custom"] = "video"
    location_detail: str = Field(default="", max_length=200)
    color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    active: bool = True
    questions: list[EventQuestion] = Field(default_factory=list, max_length=5)


class EventTypeUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=600)
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    location_type: Literal["phone", "video", "in_person", "custom"] | None = None
    location_detail: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    active: bool | None = None
    questions: list[EventQuestion] | None = Field(default=None, max_length=5)


class EventTypeOut(EventTypeCreate):
    id: str
    product_id: str = ""
    owner_id: str
    owner_slug: str
    slug: str
    public_path: str
    created_at: datetime
    updated_at: datetime


class SlotOut(BaseModel):
    start_utc: datetime
    end_utc: datetime
    local_date: date
    local_time: str
    label: str


class BookingCreatePublic(BaseModel):
    start_utc: datetime
    invitee_name: str = Field(min_length=2, max_length=80)
    invitee_email: EmailStr
    invitee_timezone: str = Field(default="Asia/Kolkata", min_length=2, max_length=80)
    invitee_message: str = Field(default="", max_length=800)
    answers: dict[str, str] = Field(default_factory=dict)


class BookingOut(BaseModel):
    id: str
    booking_code: str
    product_id: str = ""
    owner_id: str
    event_type_id: str
    event_title: str
    event_slug: str
    status: Literal["scheduled", "cancelled"]
    start_utc: datetime
    end_utc: datetime
    invitee_name: str
    invitee_email: EmailStr
    invitee_timezone: str
    invitee_message: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    cancellation_reason: str = ""
    created_at: datetime
    updated_at: datetime


class CancelBookingRequest(BaseModel):
    reason: str = Field(default="", max_length=300)


class ContactCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    company: str = Field(default="", max_length=120)
    job_title: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=800)


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    company: str | None = Field(default=None, max_length=120)
    job_title: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=800)


class ContactOut(ContactCreate):
    id: str
    product_id: str = ""
    owner_id: str
    source: Literal["manual", "booking"]
    booking_count: int = 0
    last_booking_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
