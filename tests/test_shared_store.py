import sqlite3
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

    def test_projects_isolate_pages_versions_and_sources(self):
        self.store.create_project("jetbao", "JetBao", "member-a")
        self.store.commit_update("member-a", 0, "company-policy", self.materials, update())
        project_materials = [{"source_id": "file:policy.md@sha256:jetbao", "kind": "file", "label": "policy.md", "content": "JetBao policy."}]
        self.store.commit_update("member-a", 0, "jetbao-policy", project_materials, update(source_id=project_materials[0]["source_id"], body="JetBao policy."), project_id="jetbao")

        self.assertEqual(self.store.current_version("company"), 1)
        self.assertEqual(self.store.current_version("jetbao"), 1)
        self.assertEqual(self.store.get_page("policy", "company")["page"]["body"], "Use the policy.")
        self.assertEqual(self.store.get_page("policy", "jetbao")["page"]["body"], "JetBao policy.")
        self.assertEqual([item["id"] for item in self.store.list_projects()], ["company", "jetbao"])
        self.assertEqual(self.store.list_pages("company")["pages"][0]["project_id"], "company")

    def test_legacy_single_scope_database_migrates_to_company_project(self):
        database = Path(self.temporary.name) / "legacy.sqlite3"
        db = sqlite3.connect(database)
        db.executescript("""
            CREATE TABLE wiki_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO wiki_meta VALUES ('current_version', '2');
            CREATE TABLE wiki_sources (source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL, actor_subject TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE wiki_pages (slug TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL, source_ids_json TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE wiki_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL, source_ids_json TEXT NOT NULL, actor_subject TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL, previous_version INTEGER);
            CREATE INDEX wiki_versions_slug ON wiki_versions(slug, version DESC);
            CREATE TABLE wiki_audits (id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL, action TEXT NOT NULL, actor_subject TEXT NOT NULL, summary TEXT NOT NULL, source_ids_json TEXT NOT NULL, before_version INTEGER NOT NULL, after_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE wiki_submissions (idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, intent_hash TEXT, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO wiki_sources VALUES ('legacy:guide', 'file', 'guide.md', 'Legacy', 'digest', 'member-a', '2026-09-16T00:00:00Z');
            INSERT INTO wiki_pages VALUES ('guide', 'Guide', 'guide', 'current', '[]', 'Legacy', 'Legacy body', '["legacy:guide"]', '2026-09-16T00:00:00Z', 2);
            INSERT INTO wiki_versions(id, version, slug, title, type, status, tags_json, summary, body, source_ids_json, actor_subject, action, created_at, previous_version) VALUES (1, 2, 'guide', 'Guide', 'guide', 'current', '[]', 'Legacy', 'Legacy body', '["legacy:guide"]', 'member-a', 'update', '2026-09-16T00:00:00Z', NULL);
            INSERT INTO wiki_audits VALUES (1, 2, 'update', 'member-a', 'Legacy update', '["legacy:guide"]', 1, 2, '2026-09-16T00:00:00Z');
            INSERT INTO wiki_submissions VALUES ('legacy-submit', 'request', 'intent', '{"version":2}', '2026-09-16T00:00:00Z');
        """)
        db.commit()
        db.close()

        migrated = SharedWikiStore(database)
        self.assertEqual(migrated.current_version(), 2)
        self.assertEqual(migrated.get_page("guide")["page"]["body"], "Legacy body")
        self.assertEqual(migrated.get_page("guide")["page"]["project_id"], "company")
        self.assertEqual(migrated.submission("legacy-submit")["result"]["version"], 2)

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
