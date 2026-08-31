import time
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException

from app.core.config import Settings, settings
from app.core.security import (
    BLOCKED_USER_STATUSES,
    TOKEN_TYPE_ACCESS,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.services import auth_tokens


class FakeCollection:
    """Minimal in-memory stand-in for the Motor collection API used by auth_tokens."""

    def __init__(self) -> None:
        self.documents: list[dict] = []
        self._next_id = 1

    def _matches(self, document: dict, query: dict) -> bool:
        return all(document.get(key) == value for key, value in query.items())

    async def insert_one(self, document: dict) -> None:
        document["_id"] = self._next_id
        self._next_id += 1
        self.documents.append(document)

    async def find_one(self, query: dict) -> dict | None:
        return next((doc for doc in self.documents if self._matches(doc, query)), None)

    async def update_one(self, query: dict, update: dict):
        for document in self.documents:
            if self._matches(document, query):
                document.update(update["$set"])
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    async def update_many(self, query: dict, update: dict):
        count = 0
        for document in self.documents:
            if self._matches(document, query):
                document.update(update["$set"])
                count += 1
        return type("Result", (), {"modified_count": count})()


class FakeDatabase:
    def __init__(self) -> None:
        self.refresh_tokens = FakeCollection()


class PasswordHashingTests(unittest.TestCase):
    def test_password_round_trip(self) -> None:
        hashed = hash_password("Correct-Horse-9")
        self.assertTrue(hashed.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password("Correct-Horse-9", hashed))

    def test_wrong_password_and_empty_hash_are_rejected(self) -> None:
        hashed = hash_password("Correct-Horse-9")
        self.assertFalse(verify_password("wrong", hashed))
        self.assertFalse(verify_password("anything", ""))


class AccessTokenTests(unittest.TestCase):
    def test_valid_token_round_trip(self) -> None:
        token = create_access_token("507f1f77bcf86cd799439011")
        payload = decode_access_token(token)
        self.assertEqual(payload["sub"], "507f1f77bcf86cd799439011")
        self.assertEqual(payload["type"], TOKEN_TYPE_ACCESS)
        self.assertIn("iat", payload)
        self.assertGreater(payload["exp"], int(time.time()))

    def test_tampered_payload_is_rejected(self) -> None:
        header, _, signature = create_access_token("507f1f77bcf86cd799439011").split(".", 2)
        forged_payload = "eyJzdWIiOiJhdHRhY2tlciIsInR5cGUiOiJhY2Nlc3MiLCJleHAiOjk5OTk5OTk5OTl9"
        with self.assertRaises(HTTPException) as caught:
            decode_access_token(f"{header}.{forged_payload}.{signature}")
        self.assertEqual(caught.exception.status_code, 401)

    def test_expired_token_is_rejected(self) -> None:
        token = create_access_token("507f1f77bcf86cd799439011", expires_delta=timedelta(seconds=-1))
        with self.assertRaises(HTTPException):
            decode_access_token(token)

    def test_wrong_token_type_is_rejected(self) -> None:
        token = create_access_token("507f1f77bcf86cd799439011", token_type="password_reset")
        with self.assertRaises(HTTPException):
            decode_access_token(token)

    def test_malformed_token_is_rejected(self) -> None:
        for value in ["", "not-a-token", "a.b", "a.b.c"]:
            with self.assertRaises(HTTPException):
                decode_access_token(value)

    def test_token_signed_with_another_secret_is_rejected(self) -> None:
        with patch.object(settings, "jwt_secret", "an-entirely-different-signing-secret"):
            foreign_token = create_access_token("507f1f77bcf86cd799439011")
        with self.assertRaises(HTTPException):
            decode_access_token(foreign_token)


class RefreshTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = FakeDatabase()
        self.patcher = patch.object(auth_tokens, "get_database", return_value=self.database)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_issued_token_is_stored_only_as_a_hash(self) -> None:
        token, _ = await auth_tokens.issue_refresh_token("user-1")
        stored = self.database.refresh_tokens.documents[0]
        self.assertNotIn(token, stored.values())
        self.assertEqual(stored["token_hash"], auth_tokens.hash_refresh_token(token))

    async def test_rotation_returns_a_new_token_and_revokes_the_old_one(self) -> None:
        token, _ = await auth_tokens.issue_refresh_token("user-1")
        rotated = await auth_tokens.rotate_refresh_token(token)
        self.assertIsNotNone(rotated)
        user_id, new_token, _ = rotated
        self.assertEqual(user_id, "user-1")
        self.assertNotEqual(new_token, token)
        self.assertIsNotNone(self.database.refresh_tokens.documents[0]["revoked_at"])

    async def test_replaying_a_rotated_token_revokes_every_session(self) -> None:
        token, _ = await auth_tokens.issue_refresh_token("user-1")
        rotated = await auth_tokens.rotate_refresh_token(token)
        self.assertIsNotNone(rotated)

        self.assertIsNone(await auth_tokens.rotate_refresh_token(token))
        self.assertTrue(all(doc["revoked_at"] is not None for doc in self.database.refresh_tokens.documents))

    async def test_unknown_and_expired_tokens_are_rejected(self) -> None:
        self.assertIsNone(await auth_tokens.rotate_refresh_token("never-issued"))

        with patch.object(auth_tokens, "refresh_token_lifetime", return_value=timedelta(seconds=-1)):
            expired, _ = await auth_tokens.issue_refresh_token("user-1")
        self.assertIsNone(await auth_tokens.rotate_refresh_token(expired))

    async def test_revoked_token_cannot_be_rotated(self) -> None:
        token, _ = await auth_tokens.issue_refresh_token("user-1")
        self.assertTrue(await auth_tokens.revoke_refresh_token(token))
        self.assertIsNone(await auth_tokens.rotate_refresh_token(token))


class SigningSecretTests(unittest.TestCase):
    def test_placeholder_and_short_secrets_are_flagged(self) -> None:
        for value in ["change-me-in-production", "replace-with-a-long-random-secret", "short"]:
            self.assertTrue(Settings(jwt_secret=value).jwt_secret_is_insecure)

    def test_long_random_secret_is_accepted(self) -> None:
        self.assertFalse(Settings(jwt_secret="k" * 48).jwt_secret_is_insecure)

    def test_invited_status_still_permits_authentication(self) -> None:
        self.assertNotIn("invited", BLOCKED_USER_STATUSES)
        self.assertIn("disabled", BLOCKED_USER_STATUSES)


if __name__ == "__main__":
    unittest.main()
