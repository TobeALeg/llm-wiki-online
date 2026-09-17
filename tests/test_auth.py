import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


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

        self.store.register_authorization_code("disabled-refresh")
        with self.assertRaisesRegex(AuthError, "disabled"):
            AuthService(self.store, FakeProvider({"subject": "menti-2", "name": "Still disabled"})).login_with_code("disabled-refresh")
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

    def test_existing_auth_database_is_migrated_without_losing_tokens(self):
        database = Path(self.temporary.name) / "legacy.sqlite3"
        # `with sqlite3.connect(...)` commits but does not close, so the file stays
        # locked and the temporary directory cannot be removed on Windows.
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""
                CREATE TABLE mcp_tokens (
                    token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, revoked_at INTEGER
                );
                CREATE TABLE oauth_states (
                    state_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
                );
            """)
        AuthStore(database)
        with closing(sqlite3.connect(database)) as db:
            token_columns = {row[1] for row in db.execute("PRAGMA table_info(mcp_tokens)")}
            state_columns = {row[1] for row in db.execute("PRAGMA table_info(oauth_states)")}
        self.assertTrue({"credential_id", "label", "token_kind", "created_at"} <= token_columns)
        self.assertIn("return_to", state_columns)

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

    def test_reconciliation_disables_members_missing_from_authoritative_directory(self):
        self.store.upsert_member({"subject": "menti-5", "name": "Gone", "enabled": True})
        self.assertEqual(self.store.reconcile([]), 1)
        self.assertFalse(self.store.member("menti-5")["enabled"])

    def test_reconciliation_validates_the_whole_directory_before_writing(self):
        with self.assertRaises(AuthError):
            self.store.reconcile([{"subject": "menti-6", "enabled": True}, {"subject": ""}])
        self.assertIsNone(self.store.member("menti-6"))


    def test_menti_exchange_posts_a_json_body_and_maps_display_name(self):
        import json as json_module
        from llm_wiki_mcp.auth import MentiIdentityProvider

        captured = {}

        class Response:
            def read(self, _limit=None):
                return json_module.dumps(
                    {"sub": "menti-9", "subject": "menti-9", "email": "a@b.c", "display_name": "Dandi"}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["content_type"] = request.get_header("Content-type")
            captured["body"] = json_module.loads(request.data.decode())
            return Response()

        with mock.patch.dict("os.environ", {
            "MENTI_CLIENT_ID": "lw",
            "MENTI_CLIENT_SECRET": "mhs_secret",
            "MENTI_REDIRECT_URI": "https://lw.app.mentti.work/auth/callback",
        }, clear=False), mock.patch("urllib.request.urlopen", fake_urlopen):
            identity = MentiIdentityProvider("https://mentti.work/api/sso/token").exchange_code("code-1")

        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(captured["body"]["code"], "code-1")
        self.assertEqual(captured["body"]["grant_type"], "authorization_code")
        self.assertEqual(identity["subject"], "menti-9")
        self.assertEqual(identity["name"], "Dandi")


if __name__ == "__main__":
    unittest.main()
