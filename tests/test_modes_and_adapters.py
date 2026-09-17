"""The four entry points agree with each other.

`/lw` on a laptop, the browser API and the MCP server run the same use cases over
different databases. These tests hold the three properties that make that true: a
controlled extraction produces the same change set wherever it runs, local material
never lands in the shared database, and turning v2 on closes the pre-v2 page write
path instead of quietly leaving it open.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from acceptance import case  # noqa: E402

from llm_wiki_mcp import knowledge_service as knowledge_service_module  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore, ScopeError  # noqa: E402
from llm_wiki_mcp.knowledge_service import KnowledgeService, semantic_changeset  # noqa: E402
from llm_wiki_mcp.knowledge_types import Scope  # noqa: E402
from llm_wiki_mcp.shared_service import LegacyWriteRejected, SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore  # noqa: E402

CLI = REPO_ROOT / "skills" / "lw" / "scripts" / "wiki.py"
SKILL_SCRIPTS = CLI.parent

MATERIAL = (
    "# 存储选型\n\n"
    "我们仅在当前低数据量场景使用 SQLite，暂不引入 Postgres，除非单库超过 50GB。\n"
)
LOCAL_ONLY_MATERIAL = "本地专有材料标记 ZQMLOCALONLY：客户合同编号 A-991。"


def controlled_extractor():
    """A fixed extraction that only uses the evidence this run registered.

    Standing in for the model, it returns the same candidates for the same
    material every time, so any difference between two runs comes from the mode
    and not from the extraction.
    """

    def extract(context):
        spans = list(context["evidence_ids"].items())
        if not spans:
            return []
        statement = context["artifact"].normalized_text.strip().splitlines()[-1]
        return [
            {
                "statement": statement,
                "state": {
                    "knowledge_kind": "constraint",
                    "derivation": "explicit",
                    "epistemic_status": "asserted",
                },
                "conditions": ["当前低数据量场景", "除非单库超过 50GB"],
                "subjects": ["SQLite"],
                "attribution": {
                    "asserted_by": "alice",
                    "asserted_at": None,
                    "asserted_at_precision": "unknown",
                },
                "origins": [
                    {"derivation": "explicit", "evidence_refs": [spans[0][1]]},
                ],
                "support": ["evidence"],
                "topic_ids": [],
                "unit_id": "unit-1",
            }
        ]

    return extract


class ModeParityTests(unittest.TestCase):
    """X01: the same material and the same extraction give the same change set."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.local_database = root / "local" / "knowledge.sqlite3"
        self.shared_database = root / "shared" / "wiki.sqlite3"
        self.shared_database.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temporary.cleanup()

    def _service(self, database, project_id, space):
        store = ClaimStore(database, knowledge_space_id=space)
        return KnowledgeService(
            store, knowledge_space_id=space, extractor=controlled_extractor()
        ), store

    def _ingest(self, service, project_id, material, key, run_id):
        store = service.store
        return service.ingest(
            actor_subject="alice",
            project_id=project_id,
            source_inputs=[
                {
                    "source_id": "file:notes.md",
                    "kind": "file",
                    "label": "notes.md",
                    "content": material,
                }
            ],
            base_version=store.current_version(project_id),
            idempotency_key=key,
            purpose="Capture durable project knowledge.",
            run_id=run_id,
        )

    @case("X01")
    def test_a_controlled_extraction_produces_the_same_change_set_in_both_modes(self):
        local_service, local_store = self._service(self.local_database, "parity-demo", "local")
        shared_service, shared_store = self._service(self.shared_database, "parity-demo", "shared")

        local_report = self._ingest(local_service, "parity-demo", MATERIAL, "key-1", "run-local")
        shared_report = self._ingest(shared_service, "parity-demo", MATERIAL, "key-1", "run-shared")

        self.assertEqual(semantic_changeset(local_report), semantic_changeset(shared_report))
        self.assertEqual(local_report.committed, True)
        self.assertEqual(shared_report.committed, True)

        local_claims = local_store.iter_claims(Scope.of("local", "parity-demo"))
        shared_claims = shared_store.iter_claims(Scope.of("shared", "parity-demo"))
        self.assertEqual(len(local_claims), len(shared_claims))
        self.assertEqual(
            [claim["statement"] for claim in local_claims],
            [claim["statement"] for claim in shared_claims],
        )
        self.assertEqual(
            [claim["knowledge_kind"] for claim in local_claims],
            [claim["knowledge_kind"] for claim in shared_claims],
        )
        self.assertEqual(
            [claim["conditions"] for claim in local_claims],
            [claim["conditions"] for claim in shared_claims],
        )
        # The databases are separate files, so the two runs cannot have shared state.
        self.assertNotEqual(local_store.database, shared_store.database)

    @case("X01")
    def test_a_committed_claim_reaches_a_page_whose_manifest_names_it(self):
        """The service, not just the renderer, has to put claims on pages."""

        service, store = self._service(self.local_database, "parity-demo", "local")
        report = self._ingest(service, "parity-demo", MATERIAL, "key-page", "run-page")
        self.assertEqual(report.status, "completed")

        scope = Scope.of("local", "parity-demo")
        pages = store.projections(scope)
        self.assertEqual([page["slug"] for page in pages], ["project-knowledge"])
        page = store.projection("project-knowledge", scope)
        self.assertEqual(page["projection_status"], "current")
        self.assertFalse(page["dirty"])

        claim = store.iter_claims(scope)[0]
        entries = page["manifest"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["claim_version_id"], claim["claim_version_id"])
        self.assertEqual(entries[0]["claim_id"], claim["claim_id"])
        self.assertIn(claim["statement"], page["markdown"])
        self.assertIn("当前低数据量场景", page["markdown"])
        self.assertNotEqual(page["content_sha256"], "")

    @case("X01")
    def test_a_second_identical_ingest_does_not_rewrite_the_page(self):
        service, store = self._service(self.local_database, "parity-demo", "local")
        self._ingest(service, "parity-demo", MATERIAL, "key-1", "run-1")
        scope = Scope.of("local", "parity-demo")
        before = store.projection("project-knowledge", scope)["content_sha256"]
        self._ingest(service, "parity-demo", MATERIAL, "key-2", "run-2")
        after = store.projection("project-knowledge", scope)
        self.assertEqual(after["content_sha256"], before)
        self.assertEqual(after["manifest"]["entries"][0]["claim_version_id"], store.iter_claims(scope)[0]["claim_version_id"])

    @case("X01")
    def test_local_material_never_reaches_the_shared_database_or_a_module_cache(self):
        ClaimStore(self.shared_database, knowledge_space_id="shared")
        local_service, _ = self._service(self.local_database, "local-only", "local")
        self._ingest(local_service, "local-only", LOCAL_ONLY_MATERIAL, "key-local", "run-local-only")

        shared_bytes = self.shared_database.read_bytes()
        self.assertNotIn(
            "ZQMLOCALONLY".encode("utf-8"),
            shared_bytes,
            "local material must not be written into the shared database",
        )
        self.assertNotIn(
            "A-991".encode("utf-8"),
            shared_bytes,
            "local material must not be written into the shared database",
        )

        # The row check says the same thing in the store's own terms.
        shared_store = ClaimStore(self.shared_database, knowledge_space_id="shared")
        with shared_store._db() as database:
            sources = database.execute("SELECT source_id FROM sources").fetchall()
            artifacts = database.execute("SELECT artifact_id FROM parsed_artifacts").fetchall()
        self.assertEqual([row["source_id"] for row in sources], [])
        self.assertEqual([row["artifact_id"] for row in artifacts], [])

        module_strings = [
            value
            for value in vars(knowledge_service_module).values()
            if isinstance(value, str)
        ]
        for value in module_strings:
            self.assertNotIn("ZQMLOCALONLY", value, "the service module must not cache material text")
        # The local run did store it, so the absence above is about the shared side.
        self.assertTrue(self.local_database.is_file())
        self.assertIn("ZQMLOCALONLY", self.local_database.read_bytes().decode("utf-8", "ignore"))


class CleanSkillInstallTests(unittest.TestCase):
    """X02: the skill package carries the core it needs."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir(parents=True)
        self.home = Path(self.temporary.name) / "home"
        self.env = {
            **os.environ,
            "LLM_WIKI_HOME": str(self.home),
            "PYTHONPATH": "",
            "LLM_WIKI_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _bootstrap(self):
        self.assertEqual(self._run("init").returncode, 0)
        self.assertEqual(self._run("knowledge-init").returncode, 0)

    @case("X02")
    def test_the_vendored_core_loads_without_the_server_package(self):
        script = (
            "import sys, pathlib;"
            f"sys.path.insert(0, r'{SKILL_SCRIPTS}');"
            "import claim_store, knowledge_service, evidence, knowledge_types;"
            "print(claim_store.__file__);"
            "print(knowledge_service.__file__);"
            "import importlib.util as u;"
            "print('llm_wiki_mcp_importable', u.find_spec('llm_wiki_mcp') is not None)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.strip().splitlines() if line]
        self.assertIn(str(SKILL_SCRIPTS), lines[0])
        self.assertIn(str(SKILL_SCRIPTS), lines[1])
        self.assertIn(
            "llm_wiki_mcp_importable False",
            result.stdout,
            "the clean-install check only means something when the server package is absent",
        )

    @case("X02")
    def test_frozen_local_knowledge_is_readable_through_the_cli(self):
        self._bootstrap()
        prepared = self._run("knowledge-prepare", "--text", MATERIAL)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        document = json.loads(prepared.stdout)
        source_id = next(iter(document["batches"]))
        batch = document["batches"][source_id]
        chunk = next(item for item in batch["chunks"] if "SQLite" in item["text"])
        document["batches"][source_id]["claims"] = [
            {
                "statement": "仅在当前低数据量场景使用 SQLite，暂不引入 Postgres。",
                "state": {
                    "knowledge_kind": "constraint",
                    "derivation": "explicit",
                    "epistemic_status": "asserted",
                },
                "conditions": ["当前低数据量场景"],
                "subjects": ["SQLite"],
                "attribution": {
                    "asserted_by": "alice",
                    "asserted_at": None,
                    "asserted_at_precision": "unknown",
                },
                "origins": [{"derivation": "explicit", "evidence_refs": [chunk["evidence_id"]]}],
                "support": ["evidence"],
                "topic_ids": [],
            }
        ]
        candidates = self.root / "candidates.json"
        candidates.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        ingested = self._run(
            "knowledge-ingest",
            "--text",
            MATERIAL,
            "--candidates",
            str(candidates),
            "--key",
            "clean-install-1",
        )
        self.assertEqual(ingested.returncode, 0, ingested.stderr)
        report = json.loads(ingested.stdout)
        self.assertEqual(report["committed"], True)
        self.assertEqual(report["status"], "completed")

        status = self._run("knowledge-status")
        self.assertEqual(status.returncode, 0, status.stderr)
        snapshot = json.loads(status.stdout)
        self.assertEqual(snapshot["claim_count"], 1)
        self.assertIn(str(self.home), snapshot["knowledge_database"])

        recovered = self._run("knowledge-evidence", chunk["evidence_id"])
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        evidence = json.loads(recovered.stdout)
        self.assertIn("SQLite", evidence["exact_text"])
        self.assertEqual(evidence["exact_text"], chunk["text"])

        # A second clean process reads what the first one stored.
        again = self._run("knowledge-status")
        self.assertEqual(json.loads(again.stdout)["claim_count"], 1)


class LegacyWriteBoundaryTests(unittest.TestCase):
    """X04: with v2 on, a page write cannot bypass the claim layer."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SharedWikiStore(root / "wiki.sqlite3")
        self.knowledge = ClaimStore(root / "wiki.sqlite3", knowledge_space_id="shared")
        self.service = SharedWikiService(
            self.store,
            model=None,
            knowledge=self.knowledge,
            extractor=controlled_extractor(),
            knowledge_space_id="shared",
            knowledge_v2=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _page_package(self):
        return {
            "schema_version": 1,
            "pages": [
                {
                    "slug": "storage-choice",
                    "title": "Storage choice",
                    "type": "decision",
                    "status": "current",
                    "tags": [],
                    "summary": "Page written straight to the projection.",
                    "body": "The project decided to use Postgres.",
                    "sources": ["file:notes.md"],
                    "aliases": [],
                }
            ],
            "note": "legacy write",
            "source_ids": ["file:notes.md"],
        }

    def _material(self):
        return [
            {
                "source_id": "file:notes.md",
                "kind": "file",
                "label": "notes.md",
                "content": MATERIAL,
            }
        ]

    @case("X04")
    def test_a_prebuilt_pages_update_is_rejected_rather_than_committed(self):
        with self.assertRaises(LegacyWriteRejected) as raised:
            self.service.submit(
                "alice",
                0,
                "legacy-1",
                self._material(),
                "legacy write",
                update=self._page_package(),
                project_id="company",
            )
        self.assertIn("ingest", str(raised.exception))
        self.assertEqual(self.store.list_pages("company")["pages"], [])
        self.assertEqual(self.store.current_version("company"), 0)
        self.assertEqual(self.knowledge.current_version("company"), 0)

    @case("X04")
    def test_material_only_submits_go_through_claims(self):
        result = self.service.submit(
            "alice", 0, "v2-1", self._material(), "Capture durable project knowledge.", project_id="company"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["commit"]["created_claims"], 1)
        self.assertEqual(self.store.list_pages("company")["pages"], [])
        self.assertEqual(self.knowledge.current_version("company"), 1)
        claims = self.knowledge.iter_claims(Scope.of("shared", "company"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["project_id"], "company")

    @case("X04")
    def test_restoring_an_old_page_does_not_restore_the_decision(self):
        self.store.commit_update(
            "alice",
            0,
            "v1-seed",
            self._material(),
            self._page_package(),
            purpose="Seed a v1 page so a historical page version exists.",
            project_id="company",
        )
        base = self.knowledge.current_version("company")
        first = self.service.submit(
            "alice", base, "v2-1", self._material(), "Capture durable project knowledge.", project_id="company"
        )
        claim_id = first["commit"]["created_claim_ids"][0]
        page_version = self.store.page_versions("storage-choice", "company")["versions"][0]["id"]
        base = self.knowledge.current_version("company")
        with self.assertRaises(LegacyWriteRejected) as raised:
            self.service.restore("alice", "storage-choice", page_version, base, "restore-1", "company")
        self.assertIn("restore_as_manual_note", str(raised.exception))

        before = self.knowledge.get_claim(claim_id, Scope.of("shared", "company"))
        self.assertEqual(before["selected_version"]["decision_state"], None)
        self.assertEqual(self.knowledge.current_version("company"), 1)

        # The controlled path re-ingests the historical wording as material and
        # the claim layer decides what, if anything, it changes.
        manual = self.service.restore_as_manual_note(
            "alice", "storage-choice", page_version, base, "restore-manual-1", project_id="company"
        )
        self.assertIn("manual-note:", manual["source_ids"][0])
        after = self.knowledge.get_claim(claim_id, Scope.of("shared", "company"))
        self.assertEqual(after["selected_version"]["decision_state"], None)
        self.assertEqual(after["selected_version"]["epistemic_status"], "asserted")
        status = self.knowledge.source_status(manual["source_ids"][0], Scope.of("shared", "company"))
        self.assertTrue(status["found"])

    @case("X04")
    def test_the_v2_flag_is_what_closes_the_old_route(self):
        open_service = SharedWikiService(
            self.store,
            model=None,
            knowledge=self.knowledge,
            knowledge_space_id="shared",
            knowledge_v2=False,
        )
        committed = open_service.submit(
            "alice",
            0,
            "legacy-open-1",
            self._material(),
            "legacy write",
            update=self._page_package(),
            project_id="company",
        )
        self.assertIn("storage-choice", committed["changed_pages"])
        # Turning the flag on is what stops the next one, on the same store.
        with self.assertRaises(LegacyWriteRejected):
            self.service.submit(
                "alice",
                1,
                "legacy-open-2",
                self._material(),
                "legacy write",
                update=self._page_package(),
                project_id="company",
            )
        snapshot = self.store.list_pages("company")
        self.assertEqual(len(snapshot["pages"]), 1)
        self.assertEqual(any("Postgres" in page["body"] for page in snapshot["pages"]), True)

    @case("X04")
    def test_claim_reads_stay_inside_the_requested_project(self):
        self.service.submit(
            "alice", 0, "v2-1", self._material(), "Capture durable project knowledge.", project_id="company"
        )
        claim_id = self.knowledge.iter_claims(Scope.of("shared", "company"))[0]["claim_id"]
        self.store.create_project("other", "Other", "system")
        with self.assertRaises(ScopeError):
            self.knowledge.get_claim(claim_id, Scope.of("shared", "other"))


if __name__ == "__main__":
    unittest.main()
