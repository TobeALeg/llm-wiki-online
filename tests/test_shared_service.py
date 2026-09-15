import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.shared_service import SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore  # noqa: E402


class SharedServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SharedWikiStore(Path(self.temporary.name) / "wiki.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_submit_uses_fixed_shared_scope_and_returns_same_read_version(self):
        def model(payload, purpose, pages):
            return {"pages": [{
                "slug": "shared-guide", "title": "Shared guide", "type": "guide", "status": "current", "tags": [],
                "summary": "One shared guide.", "body": "Members read this guide.", "sources": ["conversation:shared"],
            }]}

        service = SharedWikiService(self.store, model)
        materials = [{"source_id": "conversation:shared", "kind": "conversation", "content": "Members read this guide."}]
        result = service.submit("member-a", 0, "first", materials, "Share the guide")
        self.assertEqual(result["version"], service.search("member-b", "shared")["version"])
        self.assertEqual(service.page("member-b", "shared-guide")["page"]["body"], "Members read this guide.")
        self.assertEqual(service.status("member-b")["page_count"], 1)


if __name__ == "__main__":
    unittest.main()
