import asyncio
import base64
import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from email_validator import EmailNotValidError, validate_email

from app.core.config import settings

logger = logging.getLogger(__name__)

HEADER_UNSAFE_RE = re.compile(r"[\r\n]+")
TEMPORARY_SENDGRID_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
SENDGRID_ACCEPTED_STATUSES = {"QUEUED", "VALIDATED"}


class EmailProviderError(Exception):
    """Raised when the configured email provider cannot accept the request."""


@dataclass(frozen=True)
class EmailAddress:
    email: str
    name: str = ""


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str
    disposition: str = "attachment"


@dataclass(frozen=True)
class SendGridEmailMessage:
    to: list[EmailAddress]
    subject: str = ""
    text_content: str = ""
    html_content: str = ""
    template_id: str = ""
    dynamic_template_data: dict[str, Any] = field(default_factory=dict)
    cc: list[EmailAddress] = field(default_factory=list)
    bcc: list[EmailAddress] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    custom_args: dict[str, str] = field(default_factory=dict)
    attachments: list[EmailAttachment] = field(default_factory=list)
    reply_to: EmailAddress | None = None


@dataclass(frozen=True)
class SendGridDeliveryResult:
    status: str
    provider_message_id: str = ""
    failure_reason: str = ""
    attempts: int = 0
    temporary: bool = False
    accepted_at: datetime | None = None


@dataclass(frozen=True)
class EmailVerificationMessage:
    email: str
    name: str
    otp: str
    expires_in_minutes: int


def mask_email(email: str) -> str:
    local, _, domain = str(email).partition("@")
    if not domain:
        return "***"
    if len(local) <= 2:
        masked_local = f"{local[:1]}***"
    else:
        masked_local = f"{local[:2]}***{local[-1:]}"
    return f"{masked_local}@{domain}"


def clean_header_value(value: str, max_length: int = 998) -> str:
    return HEADER_UNSAFE_RE.sub(" ", str(value or "")).strip()[:max_length]


def clean_template_value(value: Any) -> Any:
    if isinstance(value, str):
        return HEADER_UNSAFE_RE.sub(" ", value).strip()
    if isinstance(value, dict):
        return {clean_header_value(str(key), 100): clean_template_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_template_value(item) for item in value]
    return value


def normalize_recipient_email(email: str) -> str:
    try:
        normalized = validate_email(str(email), check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise EmailProviderError("Invalid recipient email address") from exc
    return normalized.lower()


def formatted_address(address: EmailAddress) -> dict[str, str]:
    payload = {"email": normalize_recipient_email(address.email)}
    name = clean_header_value(address.name, 120)
    if name:
        payload["name"] = name
    return payload


def first_recipient(message: SendGridEmailMessage) -> str:
    return message.to[0].email if message.to else ""


def header_value(headers: Any, name: str) -> str:
    try:
        items = dict(headers or {}).items()
    except (TypeError, ValueError):
        return ""
    for key, value in items:
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def sdk_error_status(exc: Exception) -> int:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            pass
    return 0


class SendGridEmailProvider:
    def configuration_errors(self) -> list[str]:
        required = {
            "SENDGRID_API_KEY": settings.sendgrid_api_key,
            "SENDGRID_FROM_EMAIL": settings.sendgrid_from_email,
        }
        return [name for name, value in required.items() if not value]

    def verification_enabled(self) -> bool:
        return bool(
            settings.sendgrid_email_enabled
            or (settings.email_enabled and settings.email_provider.lower() == "sendgrid")
        )

    async def send_email_verification(self, message: EmailVerificationMessage) -> SendGridDeliveryResult:
        if not self.verification_enabled():
            raise EmailProviderError("SendGrid email verification provider is disabled")
        result = await self.send_email(self._verification_email(message))
        if result.status not in SENDGRID_ACCEPTED_STATUSES:
            raise EmailProviderError(result.failure_reason or "SendGrid email provider rejected the request")
        logger.info("Verification email accepted by SendGrid for %s", mask_email(message.email))
        return result

    async def send_email(self, message: SendGridEmailMessage) -> SendGridDeliveryResult:
        missing = self.configuration_errors()
        if missing:
            logger.error("SendGrid email configuration missing: %s", ", ".join(missing))
            return SendGridDeliveryResult(
                status="FAILED",
                failure_reason="SendGrid email provider is not configured",
                attempts=0,
            )

        try:
            payload = self._payload(message)
        except EmailProviderError as exc:
            return SendGridDeliveryResult(status="FAILED", failure_reason=str(exc), attempts=0)

        max_attempts = max(1, int(settings.sendgrid_max_send_attempts))
        timeout = max(1.0, float(settings.sendgrid_timeout_seconds))
        last_reason = "SendGrid email provider request failed"

        for attempt in range(1, max_attempts + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(self._send_payload_sync, payload),
                    timeout=timeout,
                )
            except TimeoutError:
                last_reason = "SendGrid email provider timed out"
                status_code = 0
            except Exception as exc:  # noqa: BLE001 - SDK raises transport-specific exceptions.
                status_code = sdk_error_status(exc)
                last_reason = f"SendGrid returned HTTP {status_code}" if status_code else exc.__class__.__name__
            else:
                status_code = int(getattr(response, "status_code", 0) or 0)
                if 200 <= status_code < 300:
                    message_id = header_value(getattr(response, "headers", {}), "X-Message-Id")
                    return SendGridDeliveryResult(
                        status="VALIDATED" if settings.sendgrid_sandbox_mode else "QUEUED",
                        provider_message_id=message_id,
                        attempts=attempt,
                    )
                last_reason = f"SendGrid returned HTTP {status_code}"

            temporary = status_code == 0 or status_code in TEMPORARY_SENDGRID_STATUS_CODES
            if temporary and attempt < max_attempts:
                await asyncio.sleep(0.25 * attempt)
                continue

            logger.warning(
                "SendGrid request failed for %s after %s attempt(s): %s",
                mask_email(first_recipient(message)),
                attempt,
                last_reason,
            )
            return SendGridDeliveryResult(
                status="TEMPORARY_FAILURE" if temporary else "FAILED",
                failure_reason=last_reason,
                attempts=attempt,
                temporary=temporary,
            )

        return SendGridDeliveryResult(
            status="TEMPORARY_FAILURE",
            failure_reason=last_reason,
            attempts=max_attempts,
            temporary=True,
        )

    def _send_payload_sync(self, payload: dict[str, Any]) -> Any:
        from sendgrid import SendGridAPIClient

        client = SendGridAPIClient(api_key=settings.sendgrid_api_key)
        return client.client.mail.send.post(request_body=payload)

    def _payload(self, message: SendGridEmailMessage) -> dict[str, Any]:
        if not message.to:
            raise EmailProviderError("At least one recipient is required")
        if not message.template_id and not message.subject:
            raise EmailProviderError("Subject is required when a SendGrid template is not used")
        if not message.template_id and not (message.text_content or message.html_content):
            raise EmailProviderError("Email content is required when a SendGrid template is not used")

        personalization: dict[str, Any] = {"to": [formatted_address(item) for item in message.to]}
        if message.cc:
            personalization["cc"] = [formatted_address(item) for item in message.cc]
        if message.bcc:
            personalization["bcc"] = [formatted_address(item) for item in message.bcc]
        if message.custom_args:
            personalization["custom_args"] = {
                clean_header_value(key, 80): clean_header_value(value, 500)
                for key, value in message.custom_args.items()
            }

        payload: dict[str, Any] = {
            "personalizations": [personalization],
            "from": formatted_address(EmailAddress(settings.sendgrid_from_email, settings.sendgrid_from_name)),
        }

        reply_to = message.reply_to or (
            EmailAddress(settings.sendgrid_reply_to_email)
            if settings.sendgrid_reply_to_email
            else None
        )
        if reply_to:
            payload["reply_to"] = formatted_address(reply_to)

        if message.template_id:
            payload["template_id"] = clean_header_value(message.template_id, 100)
            personalization["dynamic_template_data"] = clean_template_value(message.dynamic_template_data)
        else:
            payload["subject"] = clean_header_value(message.subject)
            content: list[dict[str, str]] = []
            if message.text_content:
                content.append({"type": "text/plain", "value": str(message.text_content)})
            if message.html_content:
                content.append({"type": "text/html", "value": str(message.html_content)})
            payload["content"] = content

        if message.categories:
            payload["categories"] = [clean_header_value(item, 255) for item in message.categories[:10]]

        if message.attachments:
            payload["attachments"] = [
                {
                    "content": base64.b64encode(item.content).decode("ascii"),
                    "filename": clean_header_value(item.filename, 255),
                    "type": clean_header_value(item.content_type, 120),
                    "disposition": clean_header_value(item.disposition, 40),
                }
                for item in message.attachments
            ]

        if settings.sendgrid_sandbox_mode:
            payload["mail_settings"] = {"sandbox_mode": {"enable": True}}

        return payload

    def _verification_email(self, message: EmailVerificationMessage) -> SendGridEmailMessage:
        template_id = settings.sendgrid_verification_template_id or settings.sendgrid_template_id
        template_data = {
            "name": clean_template_value(message.name),
            "email": normalize_recipient_email(message.email),
            "otp": message.otp,
            "code": message.otp,
            "verification_code": message.otp,
            "expires_in_minutes": message.expires_in_minutes,
            "app_name": settings.sendgrid_from_name or settings.app_name,
            "company_name": settings.sendgrid_from_name or settings.app_name,
        }
        return SendGridEmailMessage(
            to=[EmailAddress(message.email, message.name)],
            subject=f"{settings.sendgrid_from_name} verification code",
            text_content=self._verification_text(message),
            html_content=self._verification_html(message),
            template_id=template_id,
            dynamic_template_data=template_data,
            categories=["calendar_booking", "email_verification"],
            custom_args={"record_type": "email_verification"},
        )

    def _verification_text(self, message: EmailVerificationMessage) -> str:
        return (
            f"Hi {clean_template_value(message.name)},\n\n"
            f"Your {settings.sendgrid_from_name} verification code is {message.otp}.\n"
            f"It expires in {message.expires_in_minutes} minutes.\n\n"
            "If you did not request this code, you can ignore this email."
        )

    def _verification_html(self, message: EmailVerificationMessage) -> str:
        safe_name = html.escape(clean_template_value(message.name))
        safe_otp = html.escape(message.otp)
        safe_app_name = html.escape(settings.sendgrid_from_name or settings.app_name)
        return (
            '<div style="font-family:Arial,sans-serif;color:#14212f;line-height:1.5">'
            f"<p>Hi {safe_name},</p>"
            f"<p>Your {safe_app_name} verification code is:</p>"
            f'<p style="font-size:28px;font-weight:700;letter-spacing:4px">{safe_otp}</p>'
            f"<p>This code expires in {message.expires_in_minutes} minutes.</p>"
            "<p>If you did not request this code, you can ignore this email.</p>"
            "</div>"
        )


def verify_sendgrid_event_signature(payload: bytes, signature: str, timestamp: str) -> bool:
    if not settings.sendgrid_event_webhook_public_key:
        return False
    try:
        from sendgrid.helpers.eventwebhook import EventWebhook

        event_webhook = EventWebhook()
        public_key = event_webhook.convert_public_key_to_ecdsa(settings.sendgrid_event_webhook_public_key)
        return bool(event_webhook.verify_signature(payload, signature, timestamp, public_key))
    except Exception as exc:  # noqa: BLE001 - fail closed for webhook security.
        logger.warning("SendGrid webhook signature verification failed: %s", exc.__class__.__name__)
        return False


def sendgrid_event_identifier(event: dict[str, Any]) -> str:
    event_id = str(event.get("sg_event_id") or "").strip()
    if event_id:
        return event_id
    serialized = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def sendgrid_message_ids(event: dict[str, Any]) -> list[str]:
    values = [str(event.get("sg_message_id") or "").strip()]
    if values[0] and "." in values[0]:
        values.append(values[0].split(".", 1)[0])
    return [value for index, value in enumerate(values) if value and value not in values[:index]]


def sendgrid_custom_arg(event: dict[str, Any], key: str) -> str:
    for container_key in ("custom_args", "unique_args"):
        container = event.get(container_key)
        if isinstance(container, dict) and container.get(key):
            return str(container[key])
    value = event.get(key)
    return str(value) if value is not None else ""


def map_sendgrid_event_status(event_name: str) -> str:
    normalized = event_name.strip().lower().replace(" ", "").replace("_", "")
    return {
        "processed": "PROCESSED",
        "delivered": "DELIVERED",
        "deferred": "DEFERRED",
        "bounce": "BOUNCED",
        "bounced": "BOUNCED",
        "dropped": "DROPPED",
        "spamreport": "SPAM_REPORT",
        "unsubscribe": "UNSUBSCRIBED",
        "groupunsubscribe": "UNSUBSCRIBED",
    }.get(normalized, event_name.strip().upper() or "UNKNOWN")


sendgrid_email_provider = SendGridEmailProvider()
