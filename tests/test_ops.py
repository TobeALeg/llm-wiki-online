import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.ops import backup_database, restore_database  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore  # noqa: E402


class OpsTests(unittest.TestCase):
    def test_backup_and_isolated_restore_preserve_shared_records(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source = root / "source.sqlite3"
        backup = root / "backup" / "wiki.sqlite3"
        restored = root / "isolated" / "wiki.sqlite3"
        store = SharedWikiStore(source)
        store.commit_update("member-1", 0, "first", [{"source_id": "conversation:one", "content": "One"}], {
            "schema_version": 1,
            "pages": [{"slug": "one", "title": "One", "type": "guide", "status": "current", "tags": [], "summary": "One", "body": "One", "sources": ["conversation:one"]}],
            "source_ids": ["conversation:one"],
        })
        backup_database(source, backup)
        restore_database(backup, restored)
        restored_store = SharedWikiStore(restored)
        self.assertEqual(restored_store.current_version(), 1)
        self.assertEqual(restored_store.get_page("one")["page"]["body"], "One")
        self.assertEqual(restored_store.audit_log()[0]["actor_subject"], "member-1")
        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
