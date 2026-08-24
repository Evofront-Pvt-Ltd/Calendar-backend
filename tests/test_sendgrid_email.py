import unittest
from types import SimpleNamespace

from app.core.config import settings
from app.services.sendgrid import (
    EmailAddress,
    SendGridEmailMessage,
    SendGridEmailProvider,
    map_sendgrid_event_status,
    sendgrid_event_identifier,
    sendgrid_message_ids,
    verify_sendgrid_event_signature,
)


class SendGridProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original = {
            "sendgrid_api_key": settings.sendgrid_api_key,
            "sendgrid_from_email": settings.sendgrid_from_email,
            "sendgrid_from_name": settings.sendgrid_from_name,
            "sendgrid_reply_to_email": settings.sendgrid_reply_to_email,
            "sendgrid_sandbox_mode": settings.sendgrid_sandbox_mode,
            "sendgrid_max_send_attempts": settings.sendgrid_max_send_attempts,
        }
        settings.sendgrid_api_key = "SG.test-key"
        settings.sendgrid_from_email = "verified@example.com"
        settings.sendgrid_from_name = "Calendar Booking"
        settings.sendgrid_reply_to_email = "support@example.com"
        settings.sendgrid_sandbox_mode = False
        settings.sendgrid_max_send_attempts = 1

    def tearDown(self) -> None:
        for key, value in self.original.items():
            setattr(settings, key, value)

    async def test_successful_send_is_queued_not_delivered(self) -> None:
        provider = SendGridEmailProvider()
        provider._send_payload_sync = lambda payload: SimpleNamespace(  # type: ignore[method-assign]
            status_code=202,
            headers={"X-Message-Id": "provider-message-id"},
        )
        result = await provider.send_email(
            SendGridEmailMessage(
                to=[EmailAddress("member@evofront.com", "Member")],
                subject="Planning",
                text_content="Hello",
            )
        )
        self.assertEqual(result.status, "QUEUED")
        self.assertEqual(result.provider_message_id, "provider-message-id")
        self.assertEqual(result.attempts, 1)

    async def test_sandbox_mode_validates_without_delivery(self) -> None:
        settings.sendgrid_sandbox_mode = True
        provider = SendGridEmailProvider()
        provider._send_payload_sync = lambda payload: SimpleNamespace(status_code=200, headers={})  # type: ignore[method-assign]
        result = await provider.send_email(
            SendGridEmailMessage(
                to=[EmailAddress("member@evofront.com")],
                subject="Sandbox",
                text_content="Validate only",
            )
        )
        self.assertEqual(result.status, "VALIDATED")

    async def test_invalid_recipient_is_rejected_before_send(self) -> None:
        provider = SendGridEmailProvider()
        result = await provider.send_email(
            SendGridEmailMessage(
                to=[EmailAddress("not-an-email")],
                subject="Invalid",
                text_content="Hello",
            )
        )
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.attempts, 0)

    async def test_temporary_failure_retries(self) -> None:
        settings.sendgrid_max_send_attempts = 2
        attempts = {"count": 0}

        def send(payload):
            attempts["count"] += 1
            return SimpleNamespace(
                status_code=500 if attempts["count"] == 1 else 202,
                headers={"X-Message-Id": "after-retry"},
            )

        provider = SendGridEmailProvider()
        provider._send_payload_sync = send  # type: ignore[method-assign]
        result = await provider.send_email(
            SendGridEmailMessage(
                to=[EmailAddress("member@evofront.com")],
                subject="Retry",
                text_content="Hello",
            )
        )
        self.assertEqual(result.status, "QUEUED")
        self.assertEqual(result.attempts, 2)

    def test_template_payload_uses_dynamic_template_data(self) -> None:
        provider = SendGridEmailProvider()
        payload = provider._payload(
            SendGridEmailMessage(
                to=[EmailAddress("member@evofront.com", "Member")],
                template_id="d-123456",
                dynamic_template_data={"name": "Member\nInjected"},
                subject="Ignored by template",
                text_content="Fallback",
            )
        )
        self.assertEqual(payload["template_id"], "d-123456")
        self.assertNotIn("content", payload)
        self.assertEqual(payload["personalizations"][0]["dynamic_template_data"]["name"], "Member Injected")

    def test_header_values_are_sanitized(self) -> None:
        provider = SendGridEmailProvider()
        payload = provider._payload(
            SendGridEmailMessage(
                to=[EmailAddress("member@evofront.com")],
                subject="Hello\r\nBcc: attacker@example.com",
                text_content="Hello",
            )
        )
        self.assertNotIn("\n", payload["subject"])
        self.assertNotIn("\r", payload["subject"])


class SendGridWebhookHelperTests(unittest.TestCase):
    def test_event_status_mapping(self) -> None:
        self.assertEqual(map_sendgrid_event_status("delivered"), "DELIVERED")
        self.assertEqual(map_sendgrid_event_status("spam report"), "SPAM_REPORT")
        self.assertEqual(map_sendgrid_event_status("group_unsubscribe"), "UNSUBSCRIBED")

    def test_event_identifier_falls_back_to_hash(self) -> None:
        event_id = sendgrid_event_identifier({"event": "processed", "timestamp": 1})
        self.assertEqual(len(event_id), 64)

    def test_message_id_root_is_included_for_webhook_matching(self) -> None:
        self.assertEqual(
            sendgrid_message_ids({"sg_message_id": "abc.filter0001.123"}),
            ["abc.filter0001.123", "abc"],
        )

    def test_webhook_signature_fails_closed_without_key(self) -> None:
        original = settings.sendgrid_event_webhook_public_key
        settings.sendgrid_event_webhook_public_key = ""
        try:
            self.assertFalse(verify_sendgrid_event_signature(b"[]", "bad", "123"))
        finally:
            settings.sendgrid_event_webhook_public_key = original
