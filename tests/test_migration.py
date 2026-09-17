"""Migration, backup, restore and rollback for the v2 knowledge layer.

The v1 fixture is built with raw SQL rather than through `SharedWikiStore`,
because what is being migrated is a database this code did not write and must
not modify. Assertions read the real databases back: the migrated rows, the
artifact a page became, and the citation that does or does not resolve.
"""

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).parent))

from acceptance import case  # noqa: E402
from llm_wiki_mcp import migrate_v2  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore  # noqa: E402
from llm_wiki_mcp.evidence import Artifact, EvidenceError, make_evidence  # noqa: E402
from llm_wiki_mcp.knowledge_types import Scope, build_change_set  # noqa: E402


KNOWLEDGE_SPACE = "local"

RELEASE_TEXT = (
    "# Release policy\n\n"
    "Deploys run on Tuesdays at 10:00 UTC.\n\n"
    "Rollback is a revert, never a hotfix.\n"
)
RETENTION_TEXT = "# Retention\n\nConversations are kept for 90 days.\n"
OPS_TEXT = "# JetBao operations\n\nNightly deploys run at 02:00.\n"

POLICY_BODY = "Deploys run on Tuesdays.\n"
RETENTION_BODY = "Conversations are kept for 90 days.\n"
ORPHAN_BODY = "Deployment practice lives in a file the vault no longer has.\n"
HAND_WRITTEN_BODY = "Release notes are written by hand each Friday.\n"
OPS_BODY = "Nightly deploys run at 02:00.\n"

V1_SCHEMA_SQL = """
CREATE TABLE wiki_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE wiki_projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE wiki_project_meta (project_id TEXT PRIMARY KEY, current_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE wiki_sources (
    project_id TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
    content TEXT NOT NULL, content_sha256 TEXT NOT NULL, actor_subject TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, source_id)
);
CREATE TABLE wiki_pages (
    project_id TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
    tags_json TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL, source_ids_json TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL, version INTEGER NOT NULL,
    PRIMARY KEY (project_id, slug)
);
CREATE TABLE wiki_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, version INTEGER NOT NULL, slug TEXT NOT NULL,
    title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, summary TEXT NOT NULL,
    body TEXT NOT NULL, source_ids_json TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',
    actor_subject TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL, previous_version INTEGER
);
CREATE TABLE wiki_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, version INTEGER NOT NULL, action TEXT NOT NULL,
    actor_subject TEXT NOT NULL, summary TEXT NOT NULL, source_ids_json TEXT NOT NULL,
    before_version INTEGER NOT NULL, after_version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE wiki_submissions (
    project_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, intent_hash TEXT,
    result_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (project_id, idempotency_key)
);
INSERT INTO wiki_meta VALUES ('current_version', '3');
INSERT INTO wiki_projects VALUES ('company', 'Company Wiki', 'system', '2026-09-01T00:00:00Z');
INSERT INTO wiki_projects VALUES ('jetbao', 'JetBao', 'member-a', '2026-09-02T00:00:00Z');
INSERT INTO wiki_project_meta VALUES ('company', 2);
INSERT INTO wiki_project_meta VALUES ('jetbao', 1);
"""

V1_SOURCES = (
    ("company", "conversation:release-policy", "conversation", "Release policy", RELEASE_TEXT, "2026-09-03T09:00:00Z"),
    ("company", "conversation:retention", "conversation", "Retention", RETENTION_TEXT, "2026-09-03T10:00:00Z"),
    ("company", "file:empty-notes.md", "file", "Empty notes", "", "2026-09-04T09:00:00Z"),
    ("jetbao", "conversation:jetbao-ops", "conversation", "JetBao operations", OPS_TEXT, "2026-09-05T09:00:00Z"),
)

V1_PAGES = (
    ("company", "hand-written", "Hand written", HAND_WRITTEN_BODY, [], "2026-09-06T12:00:00Z", 1),
    ("company", "orphan-page", "Orphan page", ORPHAN_BODY, ["file:missing.md"], "2026-09-06T11:00:00Z", 1),
    ("company", "release-policy", "Release policy", POLICY_BODY, ["conversation:release-policy"], "2026-09-06T09:00:00Z", 2),
    ("company", "retention", "Retention", RETENTION_BODY, ["conversation:retention"], "2026-09-06T10:00:00Z", 1),
    ("jetbao", "ops-guide", "Ops guide", OPS_BODY, ["conversation:jetbao-ops"], "2026-09-07T09:00:00Z", 1),
)

V1_VERSIONS = (
    ("company", "release-policy", 1, "update", "member-a", "2026-09-06T08:00:00Z", None),
    ("company", "release-policy", 2, "update", "member-b", "2026-09-06T09:00:00Z", 1),
    ("company", "retention", 1, "update", "member-a", "2026-09-06T10:00:00Z", None),
    ("company", "orphan-page", 1, "update", "member-a", "2026-09-06T11:00:00Z", None),
    ("company", "hand-written", 1, "update", "member-a", "2026-09-06T12:00:00Z", None),
    ("jetbao", "ops-guide", 1, "update", "member-c", "2026-09-07T09:00:00Z", None),
)

V1_AUDITS = (
    ("company", 1, "update", "member-a", "First release policy update.", 0, 1, "2026-09-06T08:00:00Z"),
    ("company", 2, "update", "member-b", "Second release policy update.", 1, 2, "2026-09-06T09:00:00Z"),
    ("jetbao", 1, "update", "member-c", "Ops guide created.", 0, 1, "2026-09-07T09:00:00Z"),
)

COUNTED_TABLES = (
    "sources",
    "source_revisions",
    "parsed_artifacts",
    "evidence_refs",
    "claims",
    "claim_versions",
    "legacy_map",
    "migration_runs",
)


def build_v1_database(path):
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_SCHEMA_SQL)
        connection.executemany(
            """INSERT INTO wiki_sources(project_id, source_id, kind, label, content, content_sha256, actor_subject, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'member-a', ?)""",
            [(project, source, kind, label, content, hashlib.sha256(content.encode("utf-8")).hexdigest(), created_at)
             for project, source, kind, label, content, created_at in V1_SOURCES],
        )
        connection.executemany(
            """INSERT INTO wiki_pages(project_id, slug, title, type, status, tags_json, summary, body, source_ids_json, aliases_json, updated_at, version)
               VALUES (?, ?, ?, 'guide', 'current', '[]', ?, ?, ?, '[]', ?, ?)""",
            [(project, slug, title, f"Summary for {slug}.", body, json.dumps(sources), updated_at, version)
             for project, slug, title, body, sources, updated_at, version in V1_PAGES],
        )
        connection.executemany(
            """INSERT INTO wiki_versions(project_id, version, slug, title, type, status, tags_json, summary, body, source_ids_json, aliases_json, actor_subject, action, created_at, previous_version)
               VALUES (?, ?, ?, ?, 'guide', 'current', '[]', '', '', '[]', '[]', ?, ?, ?, ?)""",
            [(project, version, slug, slug.title(), actor, action, created_at, previous)
             for project, slug, version, action, actor, created_at, previous in V1_VERSIONS],
        )
        connection.executemany(
            """INSERT INTO wiki_audits(project_id, version, action, actor_subject, summary, source_ids_json, before_version, after_version, created_at)
               VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
            V1_AUDITS,
        )
        connection.execute(
            "INSERT INTO wiki_submissions VALUES ('company', 'legacy-submit', 'request', 'intent', '{\"version\": 2}', '2026-09-06T09:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


def read_rows(path, sql, parameters=()):
    posix = Path(path).resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    connection = sqlite3.connect("file:" + posix + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def scalar(path, sql, parameters=()):
    return read_rows(path, sql, parameters)[0][0]


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table_counts(path):
    return {table: int(scalar(path, f"SELECT COUNT(*) FROM {table}")) for table in COUNTED_TABLES}


def chunk_extractor(context):
    """One asserted fact per source, quoting the chunk it was read from.

    The citation is built with `chunk_evidence`, which is the record the
    migration registers, so the claim points at text the store can resolve.
    """

    chunk = context["chunks"][0]
    citation = migrate_v2.chunk_evidence(
        project_id=context["project_id"], artifact=context["artifact"], chunk=chunk
    )
    start, end = chunk.evidence()[0]
    quotation = context["artifact"].normalized_text[start:end]
    return [
        {
            "statement": f"{context['source_id']} records: {' '.join(quotation.split())[:80]}",
            "subjects": [context["source_id"]],
            "state": {
                "knowledge_kind": "fact",
                "derivation": "explicit",
                "epistemic_status": "asserted",
            },
            "attribution": {"asserted_by": "legacy-v1"},
            "origins": [{"derivation": "explicit", "evidence_refs": [citation.evidence_id]}],
        }
    ]


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.v1 = self.root / "wiki-v1.sqlite3"
        build_v1_database(self.v1)

    def tearDown(self):
        self.temporary.cleanup()

    def migrate_into(self, name, run_id="run-1", **kwargs):
        database = self.root / name
        report = migrate_v2.migrate(
            v1_database=self.v1,
            v2_database=database,
            knowledge_space_id=KNOWLEDGE_SPACE,
            actor_subject="migration-bot",
            run_id=run_id,
            **kwargs,
        )
        return database, report


class MigrationTests(MigrationTestCase):
    @case("G01")
    def test_g01_a_v1_database_migrates_reentrantly_and_resumes_from_its_checkpoint(self):
        before = file_sha256(self.v1)

        plan = migrate_v2.plan_migration(v1_database=self.v1, knowledge_space_id=KNOWLEDGE_SPACE)
        self.assertEqual(plan["sources"], {"total": 4, "with_raw_text": 3, "without_raw_text": 1})
        self.assertEqual(plan["pages"], {"total": 5, "generated_only": 2})
        self.assertEqual(
            [(item["project_id"], item["sources"], item["pages"]) for item in plan["projects"]],
            [("company", 3, 4), ("jetbao", 1, 1)],
        )
        self.assertEqual(
            [
                (ref["project_id"], ref["legacy_id"], ref["source_id"], ref["exists"], ref["has_raw_text"])
                for ref in plan["legacy_refs"]
            ],
            [
                ("company", "orphan-page", "file:missing.md", False, False),
                ("company", "release-policy", "conversation:release-policy", True, True),
                ("company", "retention", "conversation:retention", True, True),
                ("jetbao", "ops-guide", "conversation:jetbao-ops", True, True),
            ],
        )
        self.assertEqual(
            sorted(gap["kind"] for gap in plan["gaps"]),
            ["page_source_missing", "page_without_raw_material", "source_text_empty"],
        )
        # The counts above are the v1 rows, so the dry run cannot be reading its own output.
        self.assertEqual(scalar(self.v1, "SELECT COUNT(*) FROM wiki_sources"), plan["sources"]["total"])
        self.assertEqual(
            scalar(self.v1, "SELECT COUNT(*) FROM wiki_sources WHERE TRIM(content) <> ''"),
            plan["sources"]["with_raw_text"],
        )
        self.assertEqual(scalar(self.v1, "SELECT COUNT(*) FROM wiki_pages"), plan["pages"]["total"])
        self.assertEqual(file_sha256(self.v1), before, "a dry run must not write the v1 database")

        database, first = self.migrate_into("v2-g01.sqlite3")
        self.assertTrue(first["complete"])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["claims_skipped_reason"], "extractor_not_provided")
        self.assertEqual(first["sources"]["mapped"], 3)
        self.assertEqual(first["pages"]["mapped"], 5)
        self.assertEqual(first["claims"], {"total": 0, "extracted": 0})
        self.assertEqual(
            first["safety"], {"verified_claims": 0, "adopted_decisions": 0, "checked": 0}
        )
        self.assertEqual(
            [(item["project_id"], item["mapped_sources"], item["mapped_pages"]) for item in first["projects"]],
            [("company", 2, 4), ("jetbao", 1, 1)],
        )
        for project, legacy_id in (
            ("company", "conversation:release-policy"),
            ("company", "conversation:retention"),
            ("jetbao", "conversation:jetbao-ops"),
        ):
            with self.subTest(source=legacy_id):
                resolved = migrate_v2.resolve_legacy(database, project, "source", legacy_id)
                self.assertEqual(resolved["new_id"], migrate_v2.legacy_source_id(project, legacy_id))
        self.assertEqual(
            sorted(row["project_id"] for row in read_rows(database, "SELECT project_id FROM knowledge_projects")),
            ["company", "jetbao"],
        )
        self.assertEqual(len(read_rows(database, "SELECT project_id FROM knowledge_meta")), 2)
        mapping = migrate_v2.legacy_mapping(database, "company")
        self.assertEqual(
            mapping["source"]["conversation:release-policy"],
            {"new_kind": "source", "new_id": migrate_v2.legacy_source_id("company", "conversation:release-policy")},
        )
        self.assertEqual(mapping["page"]["hand-written"]["new_kind"], "artifact")
        self.assertEqual(mapping["project"]["company"]["new_id"], "company")
        history = json.loads(
            scalar(
                database,
                "SELECT structure_json FROM parsed_artifacts WHERE artifact_id = ?",
                (mapping["page"]["release-policy"]["new_id"],),
            )
        )
        self.assertEqual([row["version"] for row in history["legacy_history"]["versions"]], [1, 2])
        self.assertEqual(file_sha256(self.v1), before, "a migration must not write the v1 database")

        counts_after_first = table_counts(database)
        second = migrate_v2.migrate(
            v1_database=self.v1,
            v2_database=database,
            knowledge_space_id=KNOWLEDGE_SPACE,
            actor_subject="migration-bot",
            run_id="run-1",
        )
        self.assertEqual(second, {**first, "resume": second["resume"]})
        self.assertEqual(second["resume"], "cursor")
        self.assertEqual(table_counts(database), counts_after_first)
        self.assertEqual(first["sources"]["mapped"], second["sources"]["mapped"])

        interrupted, partial = self.migrate_into("v2-g01-partial.sqlite3", run_id="run-partial", checkpoint=2)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["status"], "checkpointed")
        self.assertEqual(partial["sources"]["mapped"], 2)
        self.assertEqual(
            partial["pending"]["sources"],
            [{"project_id": "jetbao", "source_id": "conversation:jetbao-ops"}],
        )
        self.assertEqual(len(partial["pending"]["pages"]), 5)
        self.assertEqual(table_counts(interrupted)["sources"], 2)

        resumed = migrate_v2.migrate(
            v1_database=self.v1,
            v2_database=interrupted,
            knowledge_space_id=KNOWLEDGE_SPACE,
            actor_subject="migration-bot",
            run_id="run-partial",
            checkpoint=2,
        )
        reference, whole = self.migrate_into("v2-g01-whole.sqlite3", run_id="run-whole")
        self.assertTrue(resumed["complete"])
        self.assertEqual(resumed["resume"], "cursor")
        self.assertEqual(resumed["pending"], {"sources": [], "pages": []})
        for section in ("projects", "sources", "pages", "claims", "safety", "gaps"):
            with self.subTest(section=section):
                self.assertEqual(resumed[section], whole[section])
        self.assertEqual(table_counts(interrupted), table_counts(reference))

    @case("G02")
    def test_g02_raw_sources_are_re_extracted_and_generated_pages_carry_no_quotation(self):
        database, report = self.migrate_into("v2-g02.sqlite3", extractor=chunk_extractor)
        self.assertIsNone(report["claims_skipped_reason"])
        self.assertEqual(report["claims"], {"total": 3, "extracted": 3})
        self.assertEqual(report["safety"], {"verified_claims": 0, "adopted_decisions": 0, "checked": 3})

        extracted = migrate_v2.legacy_text(
            v2_database=database,
            knowledge_space_id=KNOWLEDGE_SPACE,
            project_id="company",
            legacy_kind="source",
            legacy_id="conversation:release-policy",
        )
        self.assertTrue(extracted["found"])
        self.assertEqual(extracted["parse_quality"], "ok")
        self.assertFalse(extracted["legacy_unverified"])
        self.assertTrue(extracted["raw_available"])
        self.assertEqual(extracted["stored_text"], RELEASE_TEXT)
        self.assertEqual(
            extracted["source_id"],
            migrate_v2.legacy_source_id("company", "conversation:release-policy"),
        )
        self.assertTrue(extracted["evidence_ids"])
        self.assertEqual(extracted["exact_text"], RELEASE_TEXT)

        generated = migrate_v2.legacy_text(
            v2_database=database,
            knowledge_space_id=KNOWLEDGE_SPACE,
            project_id="company",
            legacy_kind="page",
            legacy_id="orphan-page",
        )
        self.assertTrue(generated["found"])
        self.assertEqual(generated["parse_quality"], "legacy_generated_page")
        self.assertTrue(generated["legacy_unverified"])
        self.assertFalse(generated["raw_available"])
        self.assertEqual(generated["stored_text"], ORPHAN_BODY)
        self.assertEqual(generated["evidence_ids"], [])
        self.assertIsNone(generated["exact_text"])
        self.assertEqual(
            migrate_v2.resolve_legacy(database, "company", "page", "orphan-page")["new_kind"], "artifact"
        )

        orphan_gaps = [gap for gap in report["gaps"] if gap["legacy_id"] == "orphan-page"]
        self.assertEqual(len(orphan_gaps), 1)
        self.assertEqual(orphan_gaps[0]["kind"], "page_source_missing")
        self.assertEqual(orphan_gaps[0]["source_id"], "file:missing.md")
        self.assertEqual(orphan_gaps[0]["project_id"], "company")

        # The quote path is a registered citation. A generated page has none, and a
        # citation built on top of its artifact anyway resolves to nothing at all.
        self.assertEqual(
            int(
                scalar(
                    database,
                    "SELECT COUNT(*) FROM evidence_refs AS e JOIN parsed_artifacts AS a ON a.artifact_id = e.artifact_id WHERE a.parse_quality = ?",
                    (migrate_v2.LEGACY_PAGE_PARSE_QUALITY,),
                )
            ),
            0,
        )
        stored = read_rows(
            database, "SELECT * FROM parsed_artifacts WHERE artifact_id = ?", (generated["artifact_id"],)
        )[0]
        fabricated = make_evidence(
            project_id="company",
            artifact=Artifact(
                artifact_id=stored["artifact_id"],
                revision_id=stored["revision_id"],
                normalized_text=stored["normalized_text"],
                normalized_sha256=stored["normalized_sha256"],
                parser_name=stored["parser_name"],
                parser_version=stored["parser_version"],
                config_hash=stored["config_hash"],
                structure=json.loads(stored["structure_json"]),
                parse_quality=stored["parse_quality"],
            ),
            spans=[(0, 10)],
        )
        store = ClaimStore(database, knowledge_space_id=KNOWLEDGE_SPACE)
        with self.assertRaises(EvidenceError) as error:
            store.load_evidence(fabricated.evidence_id, Scope.of(KNOWLEDGE_SPACE, "company"))
        self.assertEqual(error.exception.code, "EVIDENCE_NOT_FOUND")

        verified = migrate_v2.verify_restore(
            database=database, knowledge_space_id=KNOWLEDGE_SPACE, project_id="company"
        )
        self.assertTrue(verified["readable"])
        self.assertEqual(
            verified["counts"],
            {
                "projects": 2,
                "sources": 6,
                "claims": 2,
                "claim_versions": 2,
                "evidence_refs": 2,
                "knowledge_version": 2,
            },
        )
        self.assertEqual(verified["evidence_verified"], 2)
        self.assertEqual(verified["evidence_failed"], 0)
        self.assertEqual(verified["failures"], [])
        self.assertEqual(
            sorted(
                row["epistemic_status"]
                for row in read_rows(database, "SELECT DISTINCT epistemic_status FROM claim_versions WHERE project_id = 'company'")
            ),
            ["asserted"],
        )
        self.assertEqual(
            [row["decision_state"] for row in read_rows(database, "SELECT DISTINCT decision_state FROM claim_versions WHERE project_id = 'company'")],
            [None],
        )

    @case("G03")
    def test_g03_rollback_reports_writes_made_after_the_backup_and_drops_them_on_restore(self):
        database, report = self.migrate_into("v2-g03.sqlite3", extractor=chunk_extractor)
        backup = self.root / "v2-g03-backup.sqlite3"
        snapshot = migrate_v2.backup_database(source=database, destination=backup)
        self.assertEqual(snapshot["source"], str(database))
        self.assertEqual(snapshot["destination"], str(backup))
        self.assertEqual(snapshot["bytes"], backup.stat().st_size)
        self.assertEqual(snapshot["sha256"], file_sha256(backup))

        citation = migrate_v2.legacy_text(
            v2_database=database,
            knowledge_space_id=KNOWLEDGE_SPACE,
            project_id="company",
            legacy_kind="source",
            legacy_id="conversation:release-policy",
        )
        store = ClaimStore(database, knowledge_space_id=KNOWLEDGE_SPACE)
        version = store.current_version("company")
        changeset = build_change_set(
            knowledge_space_id=KNOWLEDGE_SPACE,
            project_id="company",
            run_id="post-backup",
            base_version=version,
            claims=[
                {
                    "statement": "Rollback is a revert, not a hotfix, for release deploys.",
                    "state": {
                        "knowledge_kind": "decision",
                        "derivation": "explicit",
                        "epistemic_status": "asserted",
                        "decision_state": "proposed",
                    },
                    "attribution": {"asserted_by": "member-c"},
                    "origins": [
                        {"derivation": "explicit", "evidence_refs": [citation["evidence_ids"][0]]}
                    ],
                }
            ],
        )
        outcome = store.commit_changes(
            actor_subject="member-c",
            base_version=version,
            idempotency_key="post-backup-claim",
            changeset=changeset,
            run_id="post-backup",
            project_id="company",
        )
        self.assertEqual(outcome.created_claims, 1)
        new_claim_id = outcome.created_claim_ids[0]

        plan = migrate_v2.rollback_plan(v2_database=database, backup=backup)
        self.assertGreaterEqual(plan["v2_writes_since_backup"], 1)
        self.assertIn(new_claim_id, plan["new_write_ids"])
        self.assertIn(new_claim_id, plan["new_writes"]["claims"])
        self.assertIn("read_only_fallback", plan["options"])
        self.assertIn("replay_from_log", plan["options"])
        self.assertNotIn("restore_backup", plan["options"])
        self.assertTrue(plan["restore_backup_drops_writes"])
        self.assertIn("drops", plan["statement"])
        self.assertIn("not a way to take writes back", plan["after_cutover"])
        rendered = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("lossless", rendered.lower())
        self.assertNotIn("无损", rendered)

        fresh = self.root / "v2-g03-restored.sqlite3"
        restored = migrate_v2.restore_database(backup=backup, destination=fresh)
        self.assertEqual(restored["sha256"], snapshot["sha256"])
        self.assertEqual(restored["backup"], str(backup))
        with self.assertRaises(migrate_v2.MigrationError):
            migrate_v2.restore_database(backup=backup, destination=fresh)
        again = migrate_v2.restore_database(backup=backup, destination=fresh, overwrite=True)
        self.assertTrue(again["overwritten"])

        verified = migrate_v2.verify_restore(
            database=fresh, knowledge_space_id=KNOWLEDGE_SPACE, project_id="company"
        )
        self.assertTrue(verified["readable"])
        self.assertEqual(verified["counts"]["claims"], report["claims"]["total"])
        self.assertEqual(verified["counts"]["evidence_refs"], 2)
        self.assertEqual(verified["evidence_verified"], 2)
        self.assertEqual(verified["evidence_failed"], 0)
        self.assertEqual(verified["failures"], [])
        self.assertEqual(
            scalar(fresh, "SELECT COUNT(*) FROM claims WHERE claim_id = ?", (new_claim_id,)), 0
        )
        self.assertEqual(
            scalar(database, "SELECT COUNT(*) FROM claims WHERE claim_id = ?", (new_claim_id,)), 1
        )

    @case("G03")
    def test_g03_a_backup_with_no_later_writes_offers_only_restore(self):
        database, _ = self.migrate_into("v2-g03-clean.sqlite3", extractor=chunk_extractor)
        backup = self.root / "v2-g03-clean-backup.sqlite3"
        migrate_v2.backup_database(source=database, destination=backup)
        plan = migrate_v2.rollback_plan(v2_database=database, backup=backup)
        self.assertEqual(plan["v2_writes_since_backup"], 0)
        self.assertEqual(plan["new_write_ids"], [])
        self.assertEqual(plan["new_writes"], {})
        self.assertEqual(plan["options"], ["restore_backup"])
        self.assertFalse(plan["restore_backup_drops_writes"])
        rendered = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("lossless", rendered.lower())
        self.assertNotIn("无损", rendered)


if __name__ == "__main__":
    unittest.main()
