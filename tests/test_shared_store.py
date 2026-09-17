import json
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


class PageCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SharedWikiStore(Path(self.temporary.name) / "wiki.sqlite3")
        self.materials = [{"source_id": "file:policy.md@sha256:abc123", "kind": "file", "label": "policy.md", "content": "Use the policy."}]

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, body="Use the policy.", aliases=None):
        page = update(body=body)
        if aliases is not None:
            page["pages"][0]["aliases"] = aliases
        self.store.commit_update("member-a", self.store.current_version(), f"key-{self.store.current_version()}", self.materials, page)

    def test_the_catalog_carries_identity_and_never_a_page_body(self):
        self.commit(aliases=["Policy doc", "运维政策"])
        catalog = self.store.list_page_catalog()
        self.assertEqual(catalog["version"], 1)
        self.assertEqual(len(catalog["entries"]), 1)
        entry = catalog["entries"][0]
        self.assertEqual(entry["slug"], "policy")
        self.assertEqual(entry["aliases"], ["Policy doc", "运维政策"])
        self.assertEqual(entry["type"], "guide")
        self.assertIn("summary", entry)
        self.assertNotIn("body", entry)

    def test_the_catalog_is_a_fraction_of_the_full_snapshot(self):
        self.commit(body="x" * 5_000)
        catalog_size = len(json.dumps(self.store.list_page_catalog(), ensure_ascii=False))
        snapshot_size = len(json.dumps(self.store.list_pages(), ensure_ascii=False))
        self.assertLess(catalog_size, snapshot_size)

    def test_pages_are_fetched_by_slug_in_the_requested_order(self):
        for slug, body in (("alpha", "A"), ("beta", "B")):
            page = update(body=body)
            page["pages"][0]["slug"] = slug
            page["pages"][0]["title"] = slug.title()
            self.store.commit_update("member-a", self.store.current_version(), f"key-{slug}", self.materials, page)
        result = self.store.get_pages_by_slugs(["beta", "alpha"])
        self.assertEqual([page["slug"] for page in result["pages"]], ["beta", "alpha"])
        self.assertEqual(result["missing"], [])

    def test_unknown_slugs_are_reported_rather_than_dropped(self):
        self.commit()
        result = self.store.get_pages_by_slugs(["policy", "no-such-page"])
        self.assertEqual([page["slug"] for page in result["pages"]], ["policy"])
        self.assertEqual(result["missing"], ["no-such-page"])

    def test_an_invalid_slug_is_rejected(self):
        with self.assertRaises(StoreError):
            self.store.get_pages_by_slugs(["Not A Slug"])

    def test_a_page_with_no_aliases_reads_as_an_empty_list(self):
        self.commit()
        self.assertEqual(self.store.get_page("policy")["page"]["aliases"], [])


    def test_a_restore_brings_the_aliases_back_with_the_body(self):
        # aliases live on the version row, so restoring a version has to restore them too.
        # Otherwise a page would come back under its old text with its newest names.
        page = update(body="First")
        page["pages"][0]["aliases"] = ["first-name"]
        self.store.commit_update("member-a", 0, "v1", self.materials, page)
        first_version_id = self.store.page_versions("policy")["versions"][0]["id"]
        second = update(body="Second")
        second["pages"][0]["aliases"] = ["second-name"]
        self.store.commit_update("member-b", 1, "v2", self.materials, second)
        self.assertEqual(self.store.get_page("policy")["page"]["aliases"], ["second-name"])

        self.store.restore_page("member-c", "policy", first_version_id, 2, "restore-1")
        restored = self.store.get_page("policy")["page"]
        self.assertEqual(restored["body"], "First")
        self.assertEqual(restored["aliases"], ["first-name"])


class AliasesMigrationTests(unittest.TestCase):
    """A database created before aliases existed must gain the column, not fail to open."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "wiki.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def build_pre_aliases_database(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database)
        db.executescript("""
            CREATE TABLE wiki_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO wiki_meta VALUES ('current_version', '1');
            CREATE TABLE wiki_projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE wiki_project_meta (project_id TEXT PRIMARY KEY REFERENCES wiki_projects(id) ON DELETE CASCADE, current_version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE wiki_sources (project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE, source_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL, actor_subject TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (project_id, source_id));
            CREATE TABLE wiki_pages (project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE, slug TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL, source_ids_json TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY (project_id, slug));
            CREATE TABLE wiki_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE, version INTEGER NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL, source_ids_json TEXT NOT NULL, actor_subject TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL, previous_version INTEGER);
            CREATE TABLE wiki_audits (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE, version INTEGER NOT NULL, action TEXT NOT NULL, actor_subject TEXT NOT NULL, summary TEXT NOT NULL, source_ids_json TEXT NOT NULL, before_version INTEGER NOT NULL, after_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE wiki_submissions (project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, intent_hash TEXT, result_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (project_id, idempotency_key));
            INSERT INTO wiki_projects VALUES ('company', 'Company Wiki', 'system', '2026-09-16T00:00:00Z');
            INSERT INTO wiki_project_meta VALUES ('company', 1);
            INSERT INTO wiki_pages VALUES ('company', 'policy', 'Policy', 'guide', 'current', '[]', 'The current policy.', 'Use the policy.', '["file:policy.md@sha256:abc123"]', '2026-09-16T00:00:00Z', 1);
            INSERT INTO wiki_versions(project_id, version, slug, title, type, status, tags_json, summary, body, source_ids_json, actor_subject, action, created_at, previous_version) VALUES ('company', 1, 'policy', 'Policy', 'guide', 'current', '[]', 'The current policy.', 'Use the policy.', '["file:policy.md@sha256:abc123"]', 'member-a', 'update', '2026-09-16T00:00:00Z', NULL);
        """)
        db.commit()
        db.close()

    def test_the_column_is_added_and_existing_pages_stay_readable(self):
        self.build_pre_aliases_database()
        store = SharedWikiStore(self.database)
        self.assertEqual(store.current_version(), 1)
        self.assertEqual(store.get_page("policy")["page"]["body"], "Use the policy.")
        self.assertEqual(store.get_page("policy")["page"]["aliases"], [])
        self.assertEqual(store.list_page_catalog()["entries"][0]["aliases"], [])

    def test_writing_aliases_through_the_migrated_column_works(self):
        self.build_pre_aliases_database()
        store = SharedWikiStore(self.database)
        page = update()
        page["pages"][0]["aliases"] = ["Policy doc"]
        materials = [{"source_id": "file:policy.md@sha256:abc123", "kind": "file", "label": "policy.md", "content": "Use the policy."}]
        store.commit_update("member-b", 1, "aliased", materials, page)
        self.assertEqual(store.get_page("policy")["page"]["aliases"], ["Policy doc"])
        self.assertEqual(store.get_page("policy")["page"]["version"], 2)

    def test_opening_twice_is_idempotent(self):
        self.build_pre_aliases_database()
        SharedWikiStore(self.database)
        again = SharedWikiStore(self.database)
        self.assertEqual(again.get_page("policy")["page"]["aliases"], [])


if __name__ == "__main__":
    unittest.main()
