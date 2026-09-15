import sys
import tempfile
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.shared_service import SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import ConflictError, SharedWikiStore  # noqa: E402


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

    def test_update_keeps_stable_slug_and_marks_previous_version_superseded(self):
        first_material = [{"source_id": "conversation:first", "kind": "conversation", "content": "First"}]
        first = {
            "schema_version": 1,
            "pages": [{"slug": "shared-guide", "title": "Shared guide", "type": "guide", "status": "current", "tags": [], "summary": "First", "body": "First body", "sources": ["conversation:first"]}],
            "source_ids": ["conversation:first"],
        }
        self.store.commit_update("member-a", 0, "first", first_material, first)

        def model(payload, purpose, pages):
            self.assertEqual(pages[0]["slug"], "shared-guide")
            return {"pages": [{"slug": "shared-guide", "title": "Shared guide", "type": "guide", "status": "current", "tags": [], "summary": "Second", "body": "Second body", "sources": ["conversation:second"]}]}

        service = SharedWikiService(self.store, model)
        result = service.submit("member-b", 1, "second", [{"source_id": "conversation:second", "kind": "conversation", "content": "Second"}], "Update guide")
        self.assertEqual(result["version"], 2)
        self.assertEqual(self.store.get_page("shared-guide")["page"]["body"], "Second body")
        self.assertEqual(self.store.page_versions("shared-guide")["versions"][-1]["status"], "superseded")

    def test_model_failure_does_not_mutate_shared_state(self):
        self.store.commit_update("member-a", 0, "first", [{"source_id": "conversation:first", "content": "First"}], {
            "schema_version": 1,
            "pages": [{"slug": "guide", "title": "Guide", "type": "guide", "status": "current", "tags": [], "summary": "First", "body": "First", "sources": ["conversation:first"]}],
            "source_ids": ["conversation:first"],
        })
        def failing_model(*args):
            raise RuntimeError("provider unavailable")
        with self.assertRaises(RuntimeError):
            SharedWikiService(self.store, failing_model).submit("member-b", 1, "failed", [{"source_id": "conversation:second", "content": "Second"}], "Update")
        self.assertEqual(self.store.current_version(), 1)
        self.assertEqual(self.store.get_page("guide")["page"]["body"], "First")

    def test_competing_updates_from_one_snapshot_have_one_winner(self):
        self.store.commit_update("member-a", 0, "first", [{"source_id": "conversation:first", "content": "First"}], {
            "schema_version": 1,
            "pages": [{"slug": "guide", "title": "Guide", "type": "guide", "status": "current", "tags": [], "summary": "First", "body": "First", "sources": ["conversation:first"]}],
            "source_ids": ["conversation:first"],
        })
        barrier = threading.Barrier(2)

        def model(payload, purpose, pages):
            barrier.wait(timeout=2)
            return {"pages": [{"slug": "guide", "title": "Guide", "type": "guide", "status": "current", "tags": [], "summary": "Changed", "body": purpose, "sources": ["conversation:change"]}]}

        outcomes = []
        def submit(member):
            try:
                outcomes.append(SharedWikiService(self.store, model).submit(member, 1, f"{member}-update", [{"source_id": "conversation:change", "content": member}], member))
            except ConflictError as exc:
                outcomes.append(exc)

        first = threading.Thread(target=submit, args=("member-a",))
        second = threading.Thread(target=submit, args=("member-b",))
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)
        self.assertEqual(self.store.current_version(), 2)


if __name__ == "__main__":
    unittest.main()
