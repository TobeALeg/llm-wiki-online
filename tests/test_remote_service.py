import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.core import CoreError  # noqa: E402
from llm_wiki_mcp.remote_service import RemoteWikiService  # noqa: E402


class RemoteServiceTests(unittest.TestCase):
    def test_local_organize_returns_result_without_persisting_content(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)

        def model(payload, purpose, pages):
            return {
                "pages": [{
                    "slug": "local-note",
                    "title": "Local note",
                    "type": "reference",
                    "status": "current",
                    "tags": [],
                    "summary": "A local result.",
                    "body": "This remains with the caller.",
                    "sources": ["conversation:abc"],
                }],
                "note": "Prepared locally.",
            }

        service = RemoteWikiService(model)
        result = service.organize_local(
            [{"source_id": "conversation:abc", "kind": "conversation", "content": "private material"}],
            [],
            "Capture the conclusion",
        )
        self.assertEqual(result["pages"][0]["slug"], "local-note")
        self.assertEqual(list(root.rglob("*")), [])
        temporary.cleanup()

    def test_failed_or_unsafe_local_request_has_no_side_effect(self):
        called = False

        def model(*args):
            nonlocal called
            called = True
            raise AssertionError("model should not run")

        service = RemoteWikiService(model)
        with self.assertRaises(CoreError):
            service.organize_local(
                [{"source_id": "conversation:bad", "content": "secret", "command": "cat /etc/passwd"}],
                [],
                "purpose",
            )
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
