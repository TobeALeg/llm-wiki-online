import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.auth import AuthService, AuthStore  # noqa: E402
from llm_wiki_mcp.remote_mcp import SubjectBoundTokenVerifier, create_remote_mcp  # noqa: E402
from llm_wiki_mcp.server import create_company_mcp  # noqa: E402


class FakeProvider:
    def exchange_code(self, code):
        return {"subject": "member-1", "email": "one@example.com", "name": "One"}


class RemoteMcpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.auth = AuthService(AuthStore(Path(self.temporary.name) / "auth.sqlite3"), FakeProvider())
        self.auth.store.register_authorization_code("login")
        self.login = self.auth.login_with_code("login")
        self.credential = self.auth.issue_mcp_token(self.login["session_token"])

    def tearDown(self):
        self.temporary.cleanup()

    def test_verifier_binds_mcp_access_to_stored_subject(self):
        access = asyncio.run(SubjectBoundTokenVerifier(self.auth).verify_token(self.credential["access_token"]))
        self.assertIsNotNone(access)
        self.assertEqual(access.client_id, "member-1")
        self.assertIsNone(asyncio.run(SubjectBoundTokenVerifier(self.auth).verify_token("forged")))

    def test_remote_tool_has_no_identity_override_argument(self):
        server = create_remote_mcp(self.auth, lambda subject: {"subject": subject, "ok": True})
        tool = server._tool_manager._tools["company_wiki_status"]
        self.assertNotIn("subject", tool.parameters["properties"])
        self.assertEqual(tool.parameters["properties"], {})
        self.assertEqual(server.settings.streamable_http_path, "/mcp")

    def test_company_factory_registers_shared_read_and_write_tools(self):
        with mock.patch.dict("os.environ", {"LLM_WIKI_DATABASE": str(Path(self.temporary.name) / "company.sqlite3")}, clear=False):
            server = create_company_mcp()
        names = set(server._tool_manager._tools)
        self.assertTrue({"company_wiki_status", "company_wiki_search", "company_wiki_page", "company_wiki_submit", "company_wiki_versions", "company_wiki_restore", "local_wiki_organize"} <= names)

    def test_local_mcp_tool_has_no_storage_callback(self):
        calls = []
        server = create_remote_mcp(self.auth, lambda subject: {"subject": subject}, organize_local=lambda materials, pages, purpose: calls.append((materials, pages, purpose)) or {"pages": []})
        tool = server._tool_manager._tools["local_wiki_organize"]
        self.assertNotIn("subject", tool.parameters["properties"])
        self.assertIn("materials", tool.parameters["properties"])


if __name__ == "__main__":
    unittest.main()
