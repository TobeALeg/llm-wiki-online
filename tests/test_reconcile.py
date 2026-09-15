import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.auth import AuthService, AuthStore  # noqa: E402
from llm_wiki_mcp.reconcile import MemberReconciler  # noqa: E402


class ReconcileTests(unittest.TestCase):
    def test_startup_reconciliation_disables_missing_member_and_preserves_wiki_identity(self):
        temporary = tempfile.TemporaryDirectory()
        store = AuthStore(Path(temporary.name) / "auth.sqlite3")
        store.upsert_member({"subject": "member-1", "name": "Member", "enabled": True})
        calls = []
        reconciler = MemberReconciler(AuthService(store), lambda: calls.append(True) or [{"subject": "member-1", "name": "Former member", "enabled": False}], interval_seconds=60)
        self.assertEqual(reconciler.reconcile_once(), 1)
        self.assertFalse(store.member("member-1")["enabled"])
        self.assertEqual(store.member("member-1")["name"], "Former member")
        self.assertEqual(len(calls), 1)
        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
