from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder secrets that ship in the repo and must never sign production tokens.
INSECURE_JWT_SECRETS = frozenset(
    {
        "change-me-in-production",
        "replace-with-a-long-random-secret",
        "local-docker-secret-change-before-production",
        "ci-container-validation-not-for-production",
    }
)


class Settings(BaseSettings):
    app_name: str = "Calendar Booking API"
    environment: str = "development"
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db: str = "calendar_booking"
    jwt_secret: str = "change-me-in-production"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30
    login_max_attempts: int = 10
    login_attempt_window_minutes: int = 15
    password_reset_expire_minutes: int = 60
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    frontend_url: str = "http://localhost:3000"
    application_base_url: str = "http://127.0.0.1:3000"
    organization_id: str = "default"
    organization_email_domain: str = "evofront.com"
    email_enabled: bool = False
    email_provider: str = "sendgrid"
    notifications_enabled: bool = True
    in_app_notifications_enabled: bool = True
    default_product_timezone: str = "Asia/Kolkata"
    default_support_start_time: str = "00:00"
    default_support_end_time: str = "00:00"
    default_appointment_duration_minutes: int = 30
    public_booking_mode: str = "instant"
    booking_notification_email: str = ""
    booking_from_name: str = "Calendar Booking"
    booking_reply_to_enabled: bool = True
    sendgrid_email_enabled: bool = False
    sendgrid_api_key: str = ""
    sendgrid_mail_send_url: str = "https://api.sendgrid.com/v3/mail/send"
    sendgrid_from_email: str = ""
    sendgrid_from_name: str = "Calendar Booking"
    sendgrid_reply_to_email: str = ""
    sendgrid_template_id: str = ""
    sendgrid_verification_template_id: str = ""
    sendgrid_password_reset_template_id: str = ""
    sendgrid_meeting_invitation_template_id: str = ""
    sendgrid_booking_template_id: str = ""
    sendgrid_cancellation_template_id: str = ""
    sendgrid_reminder_template_id: str = ""
    sendgrid_sandbox_mode: bool = False
    sendgrid_timeout_seconds: float = 15.0
    sendgrid_max_send_attempts: int = 2
    sendgrid_event_webhook_enabled: bool = False
    sendgrid_event_webhook_public_key: str = ""

    google_calendar_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8001/api/integrations/google/callback"
    google_calendar_scopes: str = (
        "https://www.googleapis.com/auth/calendar.events "
        "https://www.googleapis.com/auth/calendar.freebusy"
    )
    google_calendar_id: str = "primary"
    google_token_encryption_key: str = ""
    google_oauth_state_ttl_minutes: int = 10
    google_api_timeout_seconds: float = 15.0
    google_meet_link_retry_attempts: int = 3
    google_meet_link_retry_delay_seconds: float = 0.5
    app_frontend_url: str = ""

    # MSG91 email settings are parked for future reactivation.
    # msg91_email_enabled: bool = False
    # msg91_auth_key: str = ""
    # msg91_email_api_url: str = "https://control.msg91.com/api/v5/email/send"
    # msg91_email_domain: str = ""
    # msg91_from_email: str = ""
    # msg91_from_name: str = "Calendar Booking"
    # msg91_reply_to_email: str = ""
    # msg91_email_verification_template_id: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def jwt_secret_is_insecure(self) -> bool:
        return self.jwt_secret in INSECURE_JWT_SECRETS or len(self.jwt_secret) < 32

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def sendgrid_email_configured(self) -> bool:
        return bool(
            self.sendgrid_api_key
            and self.sendgrid_mail_send_url
            and self.sendgrid_from_email
        )

    @property
    def sendgrid_verification_configured(self) -> bool:
        return bool(
            self.sendgrid_email_configured
            and (
                self.sendgrid_email_enabled
                or (self.email_enabled and self.email_provider.lower() == "sendgrid")
            )
        )

    @property
    def google_calendar_scope_list(self) -> list[str]:
        return [scope.strip() for scope in self.google_calendar_scopes.split() if scope.strip()]

    @property
    def google_calendar_configured(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
            and self.google_token_encryption_key
            and self.google_calendar_scope_list
        )

    @property
    def resolved_frontend_url(self) -> str:
        return self.app_frontend_url or self.frontend_url

    # MSG91 helper is parked for future reactivation.
    # @property
    # def msg91_email_configured(self) -> bool:
    #     return bool(
    #         self.msg91_auth_key
    #         and self.msg91_email_api_url
    #         and self.msg91_email_domain
    #         and self.msg91_from_email
    #         and self.msg91_email_verification_template_id
    #     )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
