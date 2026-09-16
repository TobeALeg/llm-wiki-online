import base64
import hashlib
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from llm_wiki_mcp.auth import AuthService, AuthStore
from llm_wiki_mcp.oauth import OAuthError, OAuthService


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        store = AuthStore(Path(self.temporary.name) / "oauth.sqlite3")
        store.upsert_member({"subject": "member-oauth", "enabled": True})
        self.auth = AuthService(store)
        self.oauth = OAuthService(self.auth, issuer="https://lw.example", access_ttl=60, refresh_ttl=600)
        self.client = self.oauth.register_client({
            "client_name": "Test MCP Client",
            "redirect_uris": ["http://127.0.0.1:17777/callback"],
        })
        self.verifier = "v" * 43
        self.challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()
        ).rstrip(b"=").decode()

    def tearDown(self):
        self.temporary.cleanup()

    def authorization(self):
        return self.oauth.begin_authorization({
            "response_type": "code",
            "client_id": self.client["client_id"],
            "redirect_uri": "http://127.0.0.1:17777/callback",
            "scope": "wiki:read wiki:write",
            "state": "state-1",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "resource": "https://lw.example/mcp",
        }, "member-oauth")

    def exchange(self, code, verifier=None):
        return self.oauth.exchange_token({
            "grant_type": "authorization_code",
            "client_id": self.client["client_id"],
            "redirect_uri": "http://127.0.0.1:17777/callback",
            "code": code,
            "code_verifier": verifier or self.verifier,
            "resource": "https://lw.example/mcp",
        })

    def test_metadata_describes_the_real_resource_and_public_client_flow(self):
        resource = self.oauth.protected_resource_metadata()
        server = self.oauth.authorization_server_metadata()
        self.assertEqual(resource["resource"], "https://lw.example/mcp")
        self.assertEqual(resource["authorization_servers"], ["https://lw.example"])
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(server["token_endpoint_auth_methods_supported"], ["none"])
        self.assertIn("refresh_token", server["grant_types_supported"])

    def test_authorization_code_uses_pkce_and_is_single_use(self):
        pending = self.authorization()
        redirect = self.oauth.finish_authorization(pending["pending_id"], "member-oauth", True)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect).query)
        self.assertEqual(query["state"], ["state-1"])
        code = query["code"][0]

        with self.assertRaisesRegex(OAuthError, "Authorization code"):
            self.exchange(code, "x" * 43)
        tokens = self.exchange(code)
        self.assertTrue(tokens["access_token"].startswith("lw_oauth_"))
        self.assertTrue(tokens["refresh_token"].startswith("lw_refresh_"))
        self.assertEqual(self.auth.authenticate_mcp_token(tokens["access_token"])["subject"], "member-oauth")
        # Production web and MCP processes use separate AuthService instances
        # over the same SQLite database.
        mcp_process_auth = AuthService(AuthStore(self.auth.store.database))
        self.assertEqual(mcp_process_auth.authenticate_mcp_token(tokens["access_token"])["subject"], "member-oauth")
        with self.assertRaisesRegex(OAuthError, "Authorization code"):
            self.exchange(code)

    def test_refresh_token_rotates_without_browser_login(self):
        pending = self.authorization()
        redirect = self.oauth.finish_authorization(pending["pending_id"], "member-oauth", True)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect).query)["code"][0]
        first = self.exchange(code)
        form = {
            "grant_type": "refresh_token",
            "client_id": self.client["client_id"],
            "refresh_token": first["refresh_token"],
            "resource": "https://lw.example/mcp",
        }
        second = self.oauth.exchange_token(form)
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        with self.assertRaisesRegex(OAuthError, "Refresh token"):
            self.oauth.exchange_token(form)

        self.oauth.revoke_token({"client_id": self.client["client_id"], "token": second["refresh_token"]})
        form["refresh_token"] = second["refresh_token"]
        with self.assertRaisesRegex(OAuthError, "Refresh token"):
            self.oauth.exchange_token(form)

    def test_rejects_unregistered_or_insecure_redirects(self):
        with self.assertRaises(OAuthError):
            self.oauth.register_client({"redirect_uris": ["http://attacker.example/callback"]})


if __name__ == "__main__":
    unittest.main()
