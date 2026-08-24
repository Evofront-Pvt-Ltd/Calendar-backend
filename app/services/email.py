import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.services.sendgrid import (
    EmailAddress,
    SendGridEmailMessage,
    clean_template_value,
    sendgrid_email_provider,
)


@dataclass(frozen=True)
class InvitationEmailMessage:
    recipient_email: str
    recipient_name: str
    organizer_name: str
    product_name: str
    title: str
    description: str
    start_time: datetime
    end_time: datetime
    timezone: str
    location: str
    meeting_url: str
    invitation_link: str
    invitation_id: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class BookingNotificationMessage:
    recipient_email: str
    recipient_name: str
    product_name: str
    client_name: str
    client_company: str
    issue_category: str
    issue_title: str
    issue_description: str
    priority: str
    start_time: datetime
    end_time: datetime
    timezone: str
    duration_minutes: int
    booking_link: str
    client_phone: str = ""
    product_reference_number: str = ""
    meeting_url: str = ""
    booking_status: str = "scheduled"
    reply_to_email: str = ""
    notification_id: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class BookingConfirmationMessage:
    recipient_email: str
    recipient_name: str
    product_name: str
    event_title: str
    organizer_name: str
    start_time: datetime
    end_time: datetime
    timezone: str
    location: str
    meeting_url: str
    confirmation_link: str
    notes: str = ""
    notification_id: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class EmailDeliveryResult:
    status: str
    provider_message_id: str = ""
    failure_reason: str = ""
    sent_at: datetime | None = None
    attempts: int = 0
    temporary: bool = False


class EmailProvider:
    async def send_meeting_invitation(self, message: InvitationEmailMessage) -> EmailDeliveryResult:
        raise NotImplementedError

    async def send_booking_notification(self, message: BookingNotificationMessage) -> EmailDeliveryResult:
        raise NotImplementedError

    async def send_booking_confirmation(self, message: BookingConfirmationMessage) -> EmailDeliveryResult:
        raise NotImplementedError


class DisabledEmailProvider(EmailProvider):
    async def send_meeting_invitation(self, message: InvitationEmailMessage) -> EmailDeliveryResult:
        return EmailDeliveryResult(
            status="PENDING_EMAIL_INTEGRATION",
            failure_reason="Invitation created. Email delivery is not enabled yet.",
        )

    async def send_booking_notification(self, message: BookingNotificationMessage) -> EmailDeliveryResult:
        return EmailDeliveryResult(
            status="PENDING_EMAIL_INTEGRATION",
            failure_reason="Booking created. Email delivery is not enabled yet.",
        )

    async def send_booking_confirmation(self, message: BookingConfirmationMessage) -> EmailDeliveryResult:
        return EmailDeliveryResult(
            status="PENDING_EMAIL_INTEGRATION",
            failure_reason="Booking created. Email delivery is not enabled yet.",
        )


def display_window(start_time: datetime, end_time: datetime, timezone: str) -> str:
    try:
        tz = ZoneInfo(timezone)
        start_local = start_time.astimezone(tz)
        end_local = end_time.astimezone(tz)
    except ZoneInfoNotFoundError:
        start_local = start_time
        end_local = end_time
    return f"{start_local:%A, %d %b %Y %I:%M %p} - {end_local:%I:%M %p} ({timezone})"


def escaped_lines(value: str) -> str:
    return "<br>".join(html.escape(line) for line in str(value or "").splitlines())


class SendGridInvitationProvider(EmailProvider):
    async def send_meeting_invitation(self, message: InvitationEmailMessage) -> EmailDeliveryResult:
        result = await sendgrid_email_provider.send_email(self._meeting_email(message))
        return EmailDeliveryResult(
            status=result.status,
            provider_message_id=result.provider_message_id,
            failure_reason=result.failure_reason,
            attempts=result.attempts,
            temporary=result.temporary,
        )

    async def send_booking_notification(self, message: BookingNotificationMessage) -> EmailDeliveryResult:
        result = await sendgrid_email_provider.send_email(self._booking_email(message))
        return EmailDeliveryResult(
            status=result.status,
            provider_message_id=result.provider_message_id,
            failure_reason=result.failure_reason,
            attempts=result.attempts,
            temporary=result.temporary,
        )

    async def send_booking_confirmation(self, message: BookingConfirmationMessage) -> EmailDeliveryResult:
        result = await sendgrid_email_provider.send_email(self._booking_confirmation_email(message))
        return EmailDeliveryResult(
            status=result.status,
            provider_message_id=result.provider_message_id,
            failure_reason=result.failure_reason,
            attempts=result.attempts,
            temporary=result.temporary,
        )

    def _meeting_email(self, message: InvitationEmailMessage) -> SendGridEmailMessage:
        when = display_window(message.start_time, message.end_time, message.timezone)
        template_data = {
            "recipient_name": clean_template_value(message.recipient_name or message.recipient_email),
            "organizer_name": clean_template_value(message.organizer_name),
            "product_name": clean_template_value(message.product_name),
            "meeting_title": clean_template_value(message.title),
            "description": clean_template_value(message.description),
            "when": when,
            "timezone": message.timezone,
            "location": clean_template_value(message.location),
            "meeting_url": message.meeting_url,
            "invitation_link": message.invitation_link,
            "app_name": settings.sendgrid_from_name or settings.app_name,
        }
        return SendGridEmailMessage(
            to=[EmailAddress(message.recipient_email, message.recipient_name)],
            subject=f"Meeting invitation: {message.title}",
            text_content=self.meeting_plain_text(message, when),
            html_content=self.meeting_html(message, when),
            template_id=settings.sendgrid_meeting_invitation_template_id,
            dynamic_template_data=template_data,
            categories=["calendar_booking", "meeting_invitation"],
            custom_args={
                "record_type": "meeting_invitation",
                "invitation_id": message.invitation_id,
                "idempotency_key": message.idempotency_key,
            },
        )

    def _booking_email(self, message: BookingNotificationMessage) -> SendGridEmailMessage:
        when = display_window(message.start_time, message.end_time, message.timezone)
        category = message.issue_category.lower()
        booking_kind = "team connection booking" if category == "team connection" else "support booking"
        template_data = {
            "recipient_name": clean_template_value(message.recipient_name or message.recipient_email),
            "product_name": clean_template_value(message.product_name),
            "client_name": clean_template_value(message.client_name),
            "client_company": clean_template_value(message.client_company),
            "client_phone": clean_template_value(message.client_phone),
            "product_reference_number": clean_template_value(message.product_reference_number),
            "issue_category": clean_template_value(message.issue_category),
            "issue_title": clean_template_value(message.issue_title),
            "issue_description": clean_template_value(message.issue_description),
            "priority": clean_template_value(message.priority),
            "when": when,
            "timezone": message.timezone,
            "duration_minutes": message.duration_minutes,
            "booking_link": message.booking_link,
            "meeting_url": message.meeting_url,
            "booking_status": clean_template_value(message.booking_status),
            "app_name": settings.sendgrid_from_name or settings.app_name,
        }
        return SendGridEmailMessage(
            to=[EmailAddress(message.recipient_email, message.recipient_name)],
            subject=f"New {message.product_name} {booking_kind}: {message.issue_title}",
            text_content=self.booking_plain_text(message, when),
            html_content=self.booking_html(message, when),
            template_id=settings.sendgrid_booking_template_id,
            dynamic_template_data=template_data,
            categories=["calendar_booking", "client_booking_notification"],
            custom_args={
                "record_type": "booking_notification",
                "notification_id": message.notification_id,
                "idempotency_key": message.idempotency_key,
            },
            reply_to=EmailAddress(message.reply_to_email) if message.reply_to_email else None,
        )

    def _booking_confirmation_email(self, message: BookingConfirmationMessage) -> SendGridEmailMessage:
        when = display_window(message.start_time, message.end_time, message.timezone)
        template_data = {
            "recipient_name": clean_template_value(message.recipient_name or message.recipient_email),
            "product_name": clean_template_value(message.product_name),
            "event_title": clean_template_value(message.event_title),
            "organizer_name": clean_template_value(message.organizer_name),
            "when": when,
            "timezone": message.timezone,
            "location": clean_template_value(message.location),
            "meeting_url": message.meeting_url,
            "confirmation_link": message.confirmation_link,
            "notes": clean_template_value(message.notes),
            "app_name": settings.sendgrid_from_name or settings.app_name,
        }
        return SendGridEmailMessage(
            to=[EmailAddress(message.recipient_email, message.recipient_name)],
            subject=f"Booking confirmed: {message.event_title}",
            text_content=self.booking_confirmation_plain_text(message, when),
            html_content=self.booking_confirmation_html(message, when),
            template_id=settings.sendgrid_booking_template_id,
            dynamic_template_data=template_data,
            categories=["calendar_booking", "booking_confirmation"],
            custom_args={
                "record_type": "booking_notification",
                "notification_id": message.notification_id,
                "idempotency_key": message.idempotency_key,
            },
        )

    @staticmethod
    def meeting_plain_text(message: InvitationEmailMessage, when: str) -> str:
        parts = [
            f"Hi {message.recipient_name or message.recipient_email},",
            "",
            f"{message.organizer_name} invited you to {message.title} for {message.product_name}.",
            f"When: {when}",
        ]
        if message.location:
            parts.append(f"Location: {message.location}")
        if message.meeting_url:
            parts.append(f"Meeting URL: {message.meeting_url}")
        if message.description:
            parts.extend(["", message.description])
        parts.extend(["", f"Open invitation: {message.invitation_link}"])
        return "\n".join(parts)

    @staticmethod
    def meeting_html(message: InvitationEmailMessage, when: str) -> str:
        title = html.escape(message.title)
        product = html.escape(message.product_name)
        recipient = html.escape(message.recipient_name or message.recipient_email)
        organizer = html.escape(message.organizer_name)
        body = [
            '<div style="font-family:Arial,sans-serif;color:#14212f;line-height:1.5">',
            f"<p>Hi {recipient},</p>",
            f"<p>{organizer} invited you to <strong>{title}</strong> for {product}.</p>",
            f"<p><strong>When:</strong> {html.escape(when)}</p>",
        ]
        if message.location:
            body.append(f"<p><strong>Location:</strong> {html.escape(message.location)}</p>")
        if message.meeting_url:
            body.append(f'<p><a href="{html.escape(message.meeting_url)}">Join meeting</a></p>')
        if message.description:
            body.append(f"<p>{escaped_lines(message.description)}</p>")
        body.append(f'<p><a href="{html.escape(message.invitation_link)}">Open invitation</a></p>')
        body.append("</div>")
        return "".join(body)

    @staticmethod
    def booking_plain_text(message: BookingNotificationMessage, when: str) -> str:
        parts = [
            f"Hi {message.recipient_name or message.recipient_email},",
            "",
            f"You have a new support booking for {message.product_name}.",
            f"Client: {message.client_name}",
        ]
        if message.client_company:
            parts.append(f"Company: {message.client_company}")
        if message.client_phone:
            parts.append(f"Phone: {message.client_phone}")
        if message.product_reference_number:
            parts.append(f"Product/reference number: {message.product_reference_number}")
        parts.extend(
            [
                f"Issue: {message.issue_title}",
                f"Category: {message.issue_category}",
                f"Priority: {message.priority}",
                f"When: {when}",
                f"Duration: {message.duration_minutes} minutes",
                f"Status: {message.booking_status}",
            ]
        )
        if message.meeting_url:
            parts.append(f"Meeting URL: {message.meeting_url}")
        if message.issue_description:
            parts.extend(["", message.issue_description])
        parts.extend(["", f"View booking: {message.booking_link}"])
        return "\n".join(parts)

    @staticmethod
    def booking_html(message: BookingNotificationMessage, when: str) -> str:
        body = [
            '<div style="font-family:Arial,sans-serif;color:#14212f;line-height:1.5">',
            f"<p>Hi {html.escape(message.recipient_name or message.recipient_email)},</p>",
            f"<p>You have a new support booking for <strong>{html.escape(message.product_name)}</strong>.</p>",
            f"<p><strong>Client:</strong> {html.escape(message.client_name)}</p>",
        ]
        if message.client_company:
            body.append(f"<p><strong>Company:</strong> {html.escape(message.client_company)}</p>")
        if message.client_phone:
            body.append(f"<p><strong>Phone:</strong> {html.escape(message.client_phone)}</p>")
        if message.product_reference_number:
            body.append(f"<p><strong>Product/reference number:</strong> {html.escape(message.product_reference_number)}</p>")
        body.extend(
            [
                f"<p><strong>Issue:</strong> {html.escape(message.issue_title)}</p>",
                f"<p><strong>Category:</strong> {html.escape(message.issue_category)}</p>",
                f"<p><strong>Priority:</strong> {html.escape(message.priority)}</p>",
                f"<p><strong>When:</strong> {html.escape(when)}</p>",
                f"<p><strong>Duration:</strong> {message.duration_minutes} minutes</p>",
                f"<p><strong>Status:</strong> {html.escape(message.booking_status)}</p>",
            ]
        )
        if message.meeting_url:
            body.append(f'<p><a href="{html.escape(message.meeting_url)}">Join meeting</a></p>')
        if message.issue_description:
            body.append(f"<p>{escaped_lines(message.issue_description)}</p>")
        body.append(f'<p><a href="{html.escape(message.booking_link)}">View booking</a></p>')
        body.append("</div>")
        return "".join(body)

    @staticmethod
    def booking_confirmation_plain_text(message: BookingConfirmationMessage, when: str) -> str:
        parts = [
            f"Hi {message.recipient_name or message.recipient_email},",
            "",
            f"Your booking is confirmed for {message.event_title}.",
            f"Product/team: {message.product_name}",
            f"When: {when}",
        ]
        if message.organizer_name:
            parts.append(f"Organizer: {message.organizer_name}")
        if message.location:
            parts.append(f"Location: {message.location}")
        if message.meeting_url:
            parts.append(f"Meeting URL: {message.meeting_url}")
        if message.notes:
            parts.extend(["", message.notes])
        if message.confirmation_link:
            parts.extend(["", f"Booking link: {message.confirmation_link}"])
        return "\n".join(parts)

    @staticmethod
    def booking_confirmation_html(message: BookingConfirmationMessage, when: str) -> str:
        body = [
            '<div style="font-family:Arial,sans-serif;color:#14212f;line-height:1.5">',
            f"<p>Hi {html.escape(message.recipient_name or message.recipient_email)},</p>",
            f"<p>Your booking is confirmed for <strong>{html.escape(message.event_title)}</strong>.</p>",
            f"<p><strong>Product/team:</strong> {html.escape(message.product_name)}</p>",
            f"<p><strong>When:</strong> {html.escape(when)}</p>",
        ]
        if message.organizer_name:
            body.append(f"<p><strong>Organizer:</strong> {html.escape(message.organizer_name)}</p>")
        if message.location:
            body.append(f"<p><strong>Location:</strong> {html.escape(message.location)}</p>")
        if message.meeting_url:
            body.append(f'<p><a href="{html.escape(message.meeting_url)}">Join meeting</a></p>')
        if message.notes:
            body.append(f"<p>{escaped_lines(message.notes)}</p>")
        if message.confirmation_link:
            body.append(f'<p><a href="{html.escape(message.confirmation_link)}">View booking</a></p>')
        body.append("</div>")
        return "".join(body)


class UnsupportedEmailProvider(EmailProvider):
    async def send_meeting_invitation(self, message: InvitationEmailMessage) -> EmailDeliveryResult:
        return EmailDeliveryResult(
            status="FAILED",
            failure_reason=f"Unsupported email provider: {settings.email_provider}",
        )

    async def send_booking_notification(self, message: BookingNotificationMessage) -> EmailDeliveryResult:
        return EmailDeliveryResult(
            status="FAILED",
            failure_reason=f"Unsupported email provider: {settings.email_provider}",
        )

    async def send_booking_confirmation(self, message: BookingConfirmationMessage) -> EmailDeliveryResult:
        return EmailDeliveryResult(
            status="FAILED",
            failure_reason=f"Unsupported email provider: {settings.email_provider}",
        )


class EmailService:
    def provider(self) -> EmailProvider:
        if not settings.email_enabled:
            return DisabledEmailProvider()
        if settings.email_provider.lower() == "sendgrid":
            return SendGridInvitationProvider()
        return UnsupportedEmailProvider()

    async def send_meeting_invitation(self, message: InvitationEmailMessage) -> EmailDeliveryResult:
        return await self.provider().send_meeting_invitation(message)

    async def send_booking_notification(self, message: BookingNotificationMessage) -> EmailDeliveryResult:
        return await self.provider().send_booking_notification(message)

    async def send_booking_confirmation(self, message: BookingConfirmationMessage) -> EmailDeliveryResult:
        return await self.provider().send_booking_confirmation(message)


email_service = EmailService()
