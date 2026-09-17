"""The adapter layer must not lose what the store said.

An adapter that reshapes a payload is how a claim's status axis, a citation's
error code, or a review's question goes missing between the store and the caller.
These tests drive the MCP tool registration and the browser routes and assert the
fields survive, and that turning v2 on does not widen anyone's write access.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from acceptance import case  # noqa: E402

from llm_wiki_mcp.auth import AuthError, AuthService, AuthStore  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore  # noqa: E402
from llm_wiki_mcp.core import CORE_SCHEMA_VERSION  # noqa: E402
from llm_wiki_mcp.knowledge_service import KnowledgeService  # noqa: E402
from llm_wiki_mcp.knowledge_types import EvidenceError, Scope  # noqa: E402
from llm_wiki_mcp.oauth import OAuthService  # noqa: E402
from llm_wiki_mcp.remote_mcp import PROTOCOL_VERSION, create_remote_mcp, typed  # noqa: E402
from llm_wiki_mcp.remote_service import RemoteWikiService  # noqa: E402
from llm_wiki_mcp.server import create_company_mcp  # noqa: E402
from llm_wiki_mcp.shared_service import SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore  # noqa: E402
from llm_wiki_mcp.webapp import NotFoundError, WikiWebApp  # noqa: E402

V2_TOOLS = (
    "company_wiki_claim",
    "company_wiki_evidence",
    "company_wiki_why",
    "company_wiki_reviews",
    "company_wiki_review_action",
)


class FakeProvider:
    def exchange_code(self, code):
        return {"subject": "member-1", "email": "one@example.com", "name": "One"}


class KnowledgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "company.sqlite3"
        self.auth = AuthService(AuthStore(root / "auth.sqlite3"), FakeProvider())
        self.auth.store.register_authorization_code("login")
        self.login = self.auth.login_with_code("login")
        self.store = SharedWikiStore(self.database)
        self.knowledge = ClaimStore(self.database, knowledge_space_id="shared")
        self.shared = SharedWikiService(
            self.store,
            model=None,
            knowledge=self.knowledge,
            knowledge_space_id="shared",
            knowledge_v2=True,
        )
        self.scope = Scope.of("shared", "company")

    def tearDown(self):
        self.temporary.cleanup()

    def _seed_claim(self):
        service = KnowledgeService(self.knowledge, knowledge_space_id="shared")
        scope = self.scope
        revision = self.knowledge.freeze_revision(
            scope=scope,
            source_id="file:notes.md",
            source_type="file",
            label="notes.md",
            raw_content="仅在当前低数据量场景使用 SQLite。\n",
        )
        from llm_wiki_mcp import chunking, evidence

        text = evidence.normalize_text("仅在当前低数据量场景使用 SQLite。\n")
        artifact = evidence.freeze_artifact(
            revision_id=revision["revision_id"],
            text=text,
            parser_name=chunking.PARSER_NAME,
            parser_version=chunking.PARSER_VERSION,
            config_hash="adapter",
            structure=chunking.artifact_structure(text),
        )
        self.knowledge.store_artifact(scope=scope, artifact=artifact, chunks=chunking.chunk_text(text))
        start = text.index("SQLite")
        record = evidence.make_evidence(
            project_id=scope.project_id, artifact=artifact, spans=[(start, start + 6)]
        )
        self.knowledge.register_evidence(record, scope=scope)
        import llm_wiki_mcp.knowledge_types as knowledge_types

        outcome = self.knowledge.commit_changes(
            actor_subject="alice",
            base_version=0,
            idempotency_key="adapter-1",
            changeset=knowledge_types.build_change_set(
                knowledge_space_id="shared",
                project_id=scope.project_id,
                run_id="run-adapter",
                base_version=0,
                claims=[
                    {
                        "statement": "仅在当前低数据量场景使用 SQLite。",
                        "state": {
                            "knowledge_kind": "constraint",
                            "derivation": "explicit",
                            "epistemic_status": "asserted",
                        },
                        "conditions": ["当前低数据量场景"],
                        "origins": [{"derivation": "explicit", "evidence_refs": [record.evidence_id]}],
                        "support": ["evidence"],
                        "topic_ids": [],
                    }
                ],
            ),
            project_id=scope.project_id,
        )
        del service
        return outcome.created_claim_ids[0], record.evidence_id

    @case("X02")
    def test_the_mcp_surface_exposes_the_v2_tools_when_wired(self):
        with mock.patch.dict(
            "os.environ", {"LLM_WIKI_DATABASE": str(self.database)}, clear=False
        ):
            server = create_company_mcp()
        names = set(server._tool_manager._tools)
        for name in V2_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, names)

    @case("X02")
    def test_a_typed_result_keeps_every_field_and_is_labelled(self):
        payload = {"claim_id": "clm_1", "decision_state": "proposed", "grounding_status": "grounded"}
        stamped = typed(payload)
        for key, value in payload.items():
            with self.subTest(field=key):
                self.assertEqual(stamped[key], value)
        self.assertEqual(stamped["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(stamped["result_type"], "knowledge")

    @case("X02")
    def test_the_browser_reports_claim_status_without_losing_an_axis(self):
        claim_id, evidence_id = self._seed_claim()
        app = WikiWebApp(self.auth, self.shared, RemoteWikiService(), OAuthService(self.auth))
        headers = {"Authorization": "Bearer " + self.auth.issue_mcp_token(self.login["session_token"])["access_token"]}

        status, _, body = app.get("/api/knowledge/claims/" + claim_id, headers)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["protocol_version"].endswith("/2"), True)
        selected = payload["selected_version"]
        for axis in (
            "knowledge_kind",
            "derivation",
            "epistemic_status",
            "lifecycle_status",
            "grounding_status",
            "decision_state",
            "question_state",
        ):
            with self.subTest(axis=axis):
                self.assertIn(axis, selected)
        self.assertEqual(selected["knowledge_kind"], "constraint")
        self.assertEqual(selected["conditions"], ["当前低数据量场景"])
        self.assertEqual(payload["origins"][0]["evidence_refs"], [evidence_id])

    @case("X02")
    def test_the_browser_carries_an_evidence_error_code_rather_than_an_empty_quote(self):
        app = WikiWebApp(self.auth, self.shared, RemoteWikiService(), OAuthService(self.auth))
        headers = {"Authorization": "Bearer " + self.auth.issue_mcp_token(self.login["session_token"])["access_token"]}
        missing = "evd_" + "0" * 64
        status, _, body = app.get("/api/knowledge/evidence/" + missing, headers)
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "EVIDENCE_NOT_FOUND")
        self.assertNotIn("exact_text", payload)

    @case("X02")
    def test_an_unauthenticated_browser_call_cannot_read_knowledge(self):
        claim_id, _evidence_id = self._seed_claim()
        app = WikiWebApp(self.auth, self.shared, RemoteWikiService(), OAuthService(self.auth))
        for route in (
            "/api/knowledge/status",
            "/api/knowledge/claims/" + claim_id,
            "/api/knowledge/reviews",
        ):
            with self.subTest(route=route):
                # The route raises so the request handler can answer 401 to a JSON
                # caller, matching every other fetch-based API route.
                with self.assertRaises(AuthError):
                    app.get(route, {})

    @case("X04")
    def test_the_adapter_layer_does_not_widen_write_access(self):
        """v2 adds read routes only. The browser still cannot write content."""

        app = WikiWebApp(self.auth, self.shared, RemoteWikiService(), OAuthService(self.auth))
        headers = {
            "Authorization": "Bearer " + self.auth.issue_mcp_token(self.login["session_token"])["access_token"],
            "Content-Type": "application/json",
        }
        for route in (
            "/api/knowledge/status",
            "/api/knowledge/claims/clm_" + "0" * 32,
            "/api/knowledge/evidence/evd_" + "0" * 64,
            "/api/knowledge/reviews",
        ):
            with self.subTest(route=route):
                # The route raises, so the handler answers 404 to a JSON caller.
                with self.assertRaises(NotFoundError):
                    app.post(route, headers, json.dumps({"action": "adopt_decision"}).encode("utf-8"))

    @case("X04")
    def test_a_disabled_member_cannot_write_even_with_a_live_credential(self):
        credential = self.auth.issue_mcp_token(self.login["session_token"])
        self.auth.store.apply_event("evt-1", {"subject": "member-1", "enabled": False}, 2)
        app = WikiWebApp(self.auth, self.shared, RemoteWikiService(), OAuthService(self.auth))
        with self.assertRaises(AuthError):
            app.get("/api/knowledge/status", {"Authorization": "Bearer " + credential["access_token"]})

    @case("X04")
    def test_the_legacy_page_route_still_reads_while_v2_writes_are_on(self):
        self.store.create_project("legacy", "Legacy", "system")
        self.store.commit_update(
            "alice",
            0,
            "legacy-seed",
            [
                {
                    "source_id": "file:old.md",
                    "kind": "file",
                    "label": "old.md",
                    "content": "The old page body.",
                }
            ],
            {
                "schema_version": CORE_SCHEMA_VERSION,
                "pages": [
                    {
                        "slug": "old-page",
                        "title": "Old page",
                        "type": "concept",
                        "status": "current",
                        "tags": [],
                        "summary": "Seeded under v1.",
                        "body": "The old page body.",
                        "sources": ["file:old.md"],
                        "aliases": [],
                    }
                ],
                "note": "seed",
                "source_ids": ["file:old.md"],
            },
            project_id="legacy",
        )
        app = WikiWebApp(self.auth, self.shared, RemoteWikiService(), OAuthService(self.auth))
        headers = {"Authorization": "Bearer " + self.auth.issue_mcp_token(self.login["session_token"])["access_token"]}
        status, _, body = app.get("/api/wiki/pages/old-page?project_id=legacy", headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["page"]["slug"], "old-page")


if __name__ == "__main__":
    unittest.main()
