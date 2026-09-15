import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.core import CoreError, WikiCore  # noqa: E402


class WikiCoreTests(unittest.TestCase):
    def test_returns_structured_update_with_traceable_sources(self):
        def model(payload, purpose, pages):
            self.assertEqual(purpose, "Keep decisions")
            self.assertEqual(payload["materials"][0]["source_id"], "file:architecture.md@sha256:abc123")
            return {
                "pages": [{
                    "slug": "storage-decision",
                    "title": "Storage decision",
                    "type": "decision",
                    "status": "current",
                    "tags": ["storage"],
                    "summary": "SQLite is the initial store.",
                    "body": "The first implementation uses SQLite.",
                    "sources": ["file:architecture.md@sha256:abc123"],
                }],
                "note": "Recorded the decision.",
            }

        package = WikiCore(model).organize(
            [{"source_id": "file:architecture.md@sha256:abc123", "kind": "file", "content": "SQLite"}],
            [],
            "Keep decisions",
        )

        self.assertEqual(package["schema_version"], 1)
        self.assertEqual(package["pages"][0]["slug"], "storage-decision")
        self.assertEqual(package["pages"][0]["sources"], ["file:architecture.md@sha256:abc123"])

    def test_rejects_paths_before_model_is_called(self):
        called = False

        def model(*args):
            nonlocal called
            called = True
            return {"pages": []}

        with self.assertRaises(CoreError):
            WikiCore(model).organize(
                [{"source_id": "file:x", "content": "safe", "path": "/etc/passwd"}],
                [],
                "purpose",
            )
        self.assertFalse(called)

    def test_rejects_unknown_source_from_model(self):
        def model(payload, purpose, pages):
            return {"pages": [{
                "slug": "bad",
                "title": "Bad",
                "type": "guide",
                "status": "current",
                "tags": [],
                "summary": "bad",
                "body": "bad",
                "sources": ["file:not-submitted@sha256:nope"],
            }]}

        with self.assertRaisesRegex(CoreError, "unknown sources"):
            WikiCore(model).organize(
                [{"source_id": "file:known@sha256:abc123", "content": "safe"}],
                [],
                "purpose",
            )

    def test_update_source_ids_only_include_sources_cited_by_returned_pages(self):
        def model(payload, purpose, pages):
            return {"pages": [{
                "slug": "new",
                "title": "New",
                "type": "guide",
                "status": "current",
                "tags": [],
                "summary": "New",
                "body": "New",
                "sources": ["conversation:new"],
            }]}

        result = WikiCore(model).organize(
            [{"source_id": "conversation:new", "content": "new"}],
            [{"slug": "old", "body": "old", "sources": ["conversation:old"]}],
            "Capture",
        )
        self.assertEqual(result["source_ids"], ["conversation:new"])


if __name__ == "__main__":
    unittest.main()
