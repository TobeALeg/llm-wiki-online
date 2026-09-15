import json
import hashlib
import hmac
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.auth import AuthError, AuthService, AuthStore  # noqa: E402
from llm_wiki_mcp.remote_service import RemoteWikiService  # noqa: E402
from llm_wiki_mcp.shared_service import SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore  # noqa: E402
from llm_wiki_mcp.webapp import NotFoundError, WikiWebApp  # noqa: E402


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database = Path(self.temporary.name) / "company.sqlite3"
        self.store = SharedWikiStore(database)
        self.auth_store = AuthStore(database)
        self.auth = AuthService(self.auth_store)
        self.auth_store.upsert_member({"subject": "member-1", "name": "Member", "enabled": True})
        self.session, _ = self.auth_store.issue_session("member-1", 3600)
        self.app = WikiWebApp(self.auth, SharedWikiService(self.store, self.model), RemoteWikiService(self.model))

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def model(payload, purpose, pages):
        return {"pages": [{"slug": "welcome", "title": "Welcome", "type": "guide", "status": "current", "tags": [], "summary": "A welcome page", "body": "Read [the guide](pages/guide.md).", "sources": [payload["materials"][0]["source_id"]]}]}

    def request(self, method, path, body=None, headers=None):
        request_headers = {"Cookie": f"lw_session={self.session}", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        if method == "GET":
            status, response_headers, content = self.app.get(path, request_headers)
        else:
            status, response_headers, content = self.app.post(path, request_headers, json.dumps(body).encode())
        return status, json.loads(content) if content and response_headers.get("Content-Type", "").startswith("application/json") else content

    def test_unauthenticated_cannot_read_and_authenticated_can_browse(self):
        with self.assertRaises(AuthError):
            self.app.get("/api/wiki/pages", {"Cookie": ""})
        self.store.commit_update("member-1", 0, "first", [{"source_id": "conversation:welcome", "content": "Welcome"}], {
            "schema_version": 1, "pages": [{"slug": "welcome", "title": "Welcome", "type": "guide", "status": "current", "tags": [], "summary": "A welcome page", "body": "Welcome", "sources": ["conversation:welcome"]}], "source_ids": ["conversation:welcome"]
        })
        status, result = self.request("GET", "/api/wiki/pages")
        self.assertEqual(status, 200)
        self.assertEqual(result["pages"][0]["slug"], "welcome")

    def test_submit_search_detail_and_local_mode(self):
        self.store.commit_update("member-1", 0, "mcp-1", [{"source_id": "conversation:welcome", "content": "Welcome"}], {
            "schema_version": 1,
            "pages": [{"slug": "welcome", "title": "Welcome", "type": "guide", "status": "current", "tags": [], "summary": "A welcome page", "body": "Welcome", "sources": ["conversation:welcome"]}],
            "source_ids": ["conversation:welcome"],
        })
        status, result = self.request("GET", "/api/wiki/search?q=welcome")
        self.assertEqual(status, 200)
        self.assertEqual(result["pages"][0]["slug"], "welcome")
        status, result = self.request("GET", "/api/wiki/pages/welcome")
        self.assertEqual(status, 200)
        self.assertEqual(result["version"], 1)
        status, result = self.request("POST", "/api/local/organize", {"purpose": "Local", "materials": [{"source_id": "conversation:private", "content": "private"}], "existing_pages": []})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.current_version(), 1)

    def test_reader_html_escapes_markdown_and_has_real_states(self):
        status, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        text = html.decode("utf-8")
        self.assertIn("escapeHtml", text)
        self.assertIn("暂无已提交页面", text)
        self.assertNotIn("innerHTML = page.body", text)

    def test_health_distinguishes_local_storage_from_unconfigured_external_dependencies(self):
        with mock.patch.dict("os.environ", {"MENTI_AUTHORIZE_URL": "", "MENTI_AUTH_CODE_URL": "", "MENTI_CLIENT_ID": "", "MENTI_CLIENT_SECRET": "", "LLM_WIKI_API_KEY": "", "DEEPSEEK_API_KEY": ""}, clear=False):
            status, _, body = self.app.get("/healthz", {})
            result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["checks"]["storage"], "ok")
        self.assertEqual(result["status"], "degraded")

    def test_login_callback_requires_the_browser_state_cookie(self):
        class Provider:
            def exchange_code(self, code):
                return {"subject": "member-login", "enabled": True}

        self.auth.provider = Provider()
        with mock.patch.dict("os.environ", {"MENTI_AUTHORIZE_URL": "https://menti.example/authorize", "MENTI_CLIENT_ID": "client", "MENTI_REDIRECT_URI": "https://lw.app.mentti.work/auth/callback"}, clear=False):
            status, headers, _ = self.app.get("/auth/login", {})
        self.assertEqual(status, 302)
        state_cookie = headers["Set-Cookie"].split(";", 1)[0]
        state = state_cookie.split("=", 1)[1]
        with self.assertRaises(AuthError):
            self.app.get(f"/auth/callback?code=login-code&state={state}", {})
        self.auth_store.register_authorization_code("login-code")
        status, headers, _ = self.app.get(f"/auth/callback?code=login-code&state={state}", {"Cookie": state_cookie})
        self.assertEqual(status, 302)
        self.assertIn("lw_session=", headers["Set-Cookie"])

    def test_signed_disable_webhook_invalidates_existing_credentials(self):
        login = self.auth_store.issue_session("member-1", 3600)[0]
        token = self.auth.issue_mcp_token(login)["access_token"]
        payload = {"event_id": "member-disabled-1", "sequence": 2, "member": {"subject": "member-1", "name": "Former", "enabled": False}}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
        with mock.patch.dict("os.environ", {"MENTI_WEBHOOK_SECRET": "webhook-secret"}, clear=False):
            status, result = self.request("POST", "/webhooks/menti/members", payload, headers={"Cookie": "", "X-Menti-Signature": signature})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "applied")
        with self.assertRaises(AuthError):
            self.auth.authenticate_session(login)
        with self.assertRaises(AuthError):
            self.auth.authenticate_mcp_token(token)

    def test_headers_are_case_insensitive_and_missing_versions_are_not_found(self):
        status, _, _ = self.app.get("/api/wiki/status", {"authorization": f"Bearer {self.auth.issue_mcp_token(self.session)['access_token']}"})
        self.assertEqual(status, 200)
        with self.assertRaises(NotFoundError):
            self.app.get("/api/wiki/pages/missing/versions", {"COOKIE": f"lw_session={self.session}"})


if __name__ == "__main__":
    unittest.main()
