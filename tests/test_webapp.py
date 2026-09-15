import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.auth import AuthError, AuthService, AuthStore  # noqa: E402
from llm_wiki_mcp.remote_service import RemoteWikiService  # noqa: E402
from llm_wiki_mcp.shared_service import SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore  # noqa: E402
from llm_wiki_mcp.webapp import WikiWebApp  # noqa: E402


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
        status, result = self.request("POST", "/api/wiki/submit", {"base_version": 0, "idempotency_key": "web-1", "purpose": "Capture", "materials": [{"source_id": "conversation:welcome", "content": "Welcome"}]})
        self.assertEqual(status, 200)
        self.assertEqual(result["version"], 1)
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


if __name__ == "__main__":
    unittest.main()
