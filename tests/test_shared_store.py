import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.store import ConflictError, SharedWikiStore, StoreError  # noqa: E402


def update(source_id="file:policy.md@sha256:abc123", body="Use the policy."):
    return {
        "schema_version": 1,
        "pages": [{
            "slug": "policy",
            "title": "Policy",
            "type": "guide",
            "status": "current",
            "tags": ["ops"],
            "summary": "The current policy.",
            "body": body,
            "sources": [source_id],
        }],
        "note": "Saved policy.",
        "source_ids": [source_id],
    }


class SharedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SharedWikiStore(Path(self.temporary.name) / "wiki.sqlite3")
        self.materials = [{"source_id": "file:policy.md@sha256:abc123", "kind": "file", "label": "policy.md", "content": "Use the policy."}]

    def tearDown(self):
        self.temporary.cleanup()

    def test_first_commit_is_atomic_and_idempotent(self):
        first = self.store.commit_update("member-a", 0, "submission-1", self.materials, update())
        retry = self.store.commit_update("member-a", 0, "submission-1", self.materials, update())
        self.assertEqual(first, retry)
        self.assertEqual(self.store.current_version(), 1)
        self.assertEqual(self.store.list_pages()["pages"][0]["slug"], "policy")
        self.assertEqual(self.store.search_pages("current policy")["version"], 1)
        self.assertEqual(self.store.get_page("policy")["page"]["body"], "Use the policy.")
        self.assertEqual(len(self.store.audit_log()), 1)

    def test_two_members_share_one_fixed_scope_and_stale_write_is_rejected(self):
        self.store.commit_update("member-a", 0, "a", self.materials, update())
        with self.assertRaises(ConflictError) as error:
            self.store.commit_update("member-b", 0, "b", self.materials, update(body="A competing update."))
        self.assertEqual(error.exception.current_version, 1)
        self.assertEqual(self.store.get_page("policy")["page"]["body"], "Use the policy.")

    def test_invalid_input_does_not_create_sources_or_pages(self):
        invalid = update(source_id="file:not-submitted@sha256:nope")
        with self.assertRaises(StoreError):
            self.store.commit_update("member-a", 0, "bad", self.materials, invalid)
        self.assertEqual(self.store.current_version(), 0)
        self.assertEqual(self.store.list_pages()["pages"], [])

    def test_server_never_accepts_a_path_as_material_input(self):
        materials = [{"source_id": "file:policy.md@sha256:abc123", "content": "safe", "path": "/etc/passwd"}]
        with self.assertRaises(StoreError):
            self.store.commit_update("member-a", 0, "bad-path", materials, update())


if __name__ == "__main__":
    unittest.main()
