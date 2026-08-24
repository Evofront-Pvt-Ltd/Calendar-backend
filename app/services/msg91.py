# MSG91 email delivery is parked for future reactivation.
# Uncomment this file together with the MSG91 blocks in app/routers/auth.py,
# app/core/config.py, docker-compose.yml, and .env.example when you want MSG91
# OTP delivery back.
#
# import logging
# from dataclasses import dataclass
#
# import httpx
#
# from app.core.config import settings
#
# logger = logging.getLogger(__name__)
#
#
# class EmailProviderError(Exception):
#     """Raised when the configured email provider cannot accept the request."""
#
#
# @dataclass(frozen=True)
# class EmailVerificationMessage:
#     email: str
#     name: str
#     otp: str
#     expires_in_minutes: int
#
#
# def mask_email(email: str) -> str:
#     local, _, domain = email.partition("@")
#     if not domain:
#         return "***"
#     if len(local) <= 2:
#         masked_local = f"{local[:1]}***"
#     else:
#         masked_local = f"{local[:2]}***{local[-1:]}"
#     return f"{masked_local}@{domain}"
#
#
# class Msg91EmailProvider:
#     def __init__(self, timeout_seconds: float = 15.0) -> None:
#         self.timeout_seconds = timeout_seconds
#
#     def configuration_errors(self) -> list[str]:
#         required = {
#             "MSG91_AUTH_KEY": settings.msg91_auth_key,
#             "MSG91_EMAIL_API_URL": settings.msg91_email_api_url,
#             "MSG91_EMAIL_DOMAIN": settings.msg91_email_domain,
#             "MSG91_FROM_EMAIL": settings.msg91_from_email,
#             "MSG91_EMAIL_VERIFICATION_TEMPLATE_ID": settings.msg91_email_verification_template_id,
#         }
#         return [name for name, value in required.items() if not value]
#
#     async def send_email_verification(self, message: EmailVerificationMessage) -> None:
#         if not settings.msg91_email_enabled:
#             raise EmailProviderError("Email verification provider is disabled")
#
#         missing = self.configuration_errors()
#         if missing:
#             logger.error("MSG91 email configuration missing: %s", ", ".join(missing))
#             raise EmailProviderError("Email verification provider is not configured")
#
#         recipient_variables = {
#             "name": message.name,
#             "email": message.email,
#             "otp": message.otp,
#             "code": message.otp,
#             "verification_code": message.otp,
#             "expires_in_minutes": str(message.expires_in_minutes),
#             "app_name": settings.app_name,
#             "company_name": settings.app_name,
#         }
#         payload: dict[str, object] = {
#             "recipients": [
#                 {
#                     "to": [{"email": message.email, "name": message.name}],
#                     "variables": recipient_variables,
#                 }
#             ],
#             "from": {"email": settings.msg91_from_email, "name": settings.msg91_from_name},
#             "domain": settings.msg91_email_domain,
#             "template_id": settings.msg91_email_verification_template_id,
#         }
#         if settings.msg91_reply_to_email:
#             payload["reply_to"] = [{"email": settings.msg91_reply_to_email}]
#
#         headers = {
#             "accept": "application/json",
#             "authkey": settings.msg91_auth_key,
#             "content-type": "application/json",
#         }
#
#         try:
#             async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
#                 response = await client.post(settings.msg91_email_api_url, headers=headers, json=payload)
#         except httpx.HTTPError as exc:
#             logger.warning("MSG91 email request failed for %s: %s", mask_email(message.email), exc.__class__.__name__)
#             raise EmailProviderError("Email verification provider request failed") from exc
#
#         if response.status_code >= 400:
#             logger.warning(
#                 "MSG91 email rejected verification request for %s with status %s",
#                 mask_email(message.email),
#                 response.status_code,
#             )
#             raise EmailProviderError("Email verification provider rejected the request")
#
#         logger.info("Verification email accepted by MSG91 for %s", mask_email(message.email))
#
#
# msg91_email_provider = Msg91EmailProvider()
