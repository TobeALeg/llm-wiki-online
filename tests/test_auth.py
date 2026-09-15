import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.auth import AuthError, AuthService, AuthStore  # noqa: E402


class FakeProvider:
    def __init__(self, identity):
        self.identity = identity

    def exchange_code(self, code):
        return dict(self.identity)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = AuthStore(Path(self.temporary.name) / "auth.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_code_is_single_use_and_subject_is_stable(self):
        self.store.register_authorization_code("one-time")
        service = AuthService(self.store, FakeProvider({"subject": "menti-1", "email": "old@example.com", "name": "Old"}))
        login = service.login_with_code("one-time")
        self.assertEqual(login["member"]["subject"], "menti-1")
        with self.assertRaises(AuthError):
            service.login_with_code("one-time")

        self.store.register_authorization_code("profile-refresh")
        refreshed = AuthService(self.store, FakeProvider({"subject": "menti-1", "email": "new@example.com", "name": "New"})).login_with_code("profile-refresh")
        self.assertEqual(refreshed["member"]["subject"], "menti-1")
        self.assertEqual(refreshed["member"]["name"], "New")
        self.assertEqual(self.store.member("menti-1")["email"], "new@example.com")

    def test_disabled_member_cannot_login_or_use_token(self):
        self.store.register_authorization_code("disabled")
        service = AuthService(self.store, FakeProvider({"subject": "menti-2", "enabled": False}))
        with self.assertRaisesRegex(AuthError, "disabled"):
            service.login_with_code("disabled")
        self.assertIsNotNone(self.store.member("menti-2"))
        self.assertFalse(self.store.member("menti-2")["enabled"])

    def test_mcp_token_is_bound_to_subject_and_revocable(self):
        self.store.register_authorization_code("valid")
        service = AuthService(self.store, FakeProvider({"subject": "menti-3", "name": "Member"}))
        login = service.login_with_code("valid")
        credential = service.issue_mcp_token(login["session_token"])
        self.assertEqual(service.authenticate_mcp_token(credential["access_token"])["subject"], "menti-3")
        service.revoke_mcp_token(credential["access_token"])
        with self.assertRaises(AuthError):
            service.authenticate_mcp_token(credential["access_token"])

    def test_webhook_signature_and_ordering(self):
        body = b'{"event":"member.disabled"}'
        import hashlib
        import hmac
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(AuthService.verify_webhook("secret", body, signature))
        self.assertFalse(AuthService.verify_webhook("wrong", body, signature))

        self.assertEqual(self.store.apply_event("evt-1", {"subject": "menti-4", "enabled": False}, 2), "applied")
        self.assertEqual(self.store.apply_event("evt-0", {"subject": "menti-4", "enabled": True}, 1), "stale")
        self.assertEqual(self.store.apply_event("evt-1", {"subject": "menti-4", "enabled": True}, 3), "duplicate")
        self.assertFalse(self.store.member("menti-4")["enabled"])


if __name__ == "__main__":
    unittest.main()
