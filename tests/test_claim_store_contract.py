"""Contract tests for the v2 ClaimStore.

The store owns the one transaction that turns a validated change set into
knowledge, and it mints the identity of everything it writes. These tests drive it
through a real database file: scope boundaries, orthogonal state axes, claim
identity and evolution, the relation and support graphs, review actions, and the
retry and rollback behaviour a crashed ingest depends on.

Every test states the acceptance case it proves with `@case`. Where the store does
not implement the promised behaviour the test fails here instead of being softened
around it.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).parent))

from acceptance import case  # noqa: E402

from llm_wiki_mcp.claim_store import (  # noqa: E402
    ClaimStore,
    ClaimStoreError,
    ConflictError,
    IdempotencyError,
    ScopeError,
    StaleReviewError,
)
from llm_wiki_mcp.evidence import (  # noqa: E402
    EvidenceError,
    freeze_artifact,
    make_evidence,
)
from llm_wiki_mcp.knowledge_types import (  # noqa: E402
    CLAIM_ID_PATTERN,
    CLAIM_VERSION_PATTERN,
    ClaimRelation,
    ClaimState,
    KnowledgeError,
    Scope,
    build_change_set,
    find_derivation_cycle,
    support_group_outcome,
    validate_claim_state,
)


SPACE = "space-main"
DEFAULT_ATTRIBUTION = {"asserted_by": "standup", "asserted_at": "2026-09-10", "asserted_at_precision": "day"}


def claim(
    statement,
    *,
    evidence,
    origins=None,
    kind="judgment",
    derivation="explicit",
    epistemic_status="asserted",
    decision_state=None,
    question_state=None,
    conditions=(),
    attribution=None,
    support=(),
    claim_id=None,
    topic_ids=(),
):
    """One claim payload for `build_change_set`, carrying only the axes a case needs."""

    state = {"knowledge_kind": kind, "derivation": derivation, "epistemic_status": epistemic_status}
    if decision_state is not None:
        state["decision_state"] = decision_state
    if question_state is not None:
        state["question_state"] = question_state
    payload = {
        "statement": statement,
        "state": state,
        "conditions": list(conditions),
        "attribution": dict(attribution) if attribution is not None else dict(DEFAULT_ATTRIBUTION),
        "origins": (
            [dict(origin) for origin in origins]
            if origins is not None
            else [{"derivation": derivation, "evidence_refs": list(evidence)}]
        ),
        "evidence_refs": list(evidence),
        "support": list(support),
        "subjects": [],
        "topic_ids": list(topic_ids),
    }
    if claim_id is not None:
        payload["claim_id"] = claim_id
    return payload


def edge(relation_type, from_version, to_version, *, relation_status="proposed", evidence_refs=()):
    """One relation payload for `build_change_set`."""

    return {
        "relation_type": relation_type,
        "from_claim_version_id": from_version,
        "to_claim_version_id": to_version,
        "relation_status": relation_status,
        "origin_evidence_refs": list(evidence_refs),
    }


def review(subject_id, subject_version, *, trigger_code="ambiguous_adoption", question="Keep this?", candidates=()):
    """One review queue entry for `build_change_set`."""

    return {
        "subject_kind": "claim",
        "subject_id": subject_id,
        "subject_version": subject_version,
        "question": question,
        "trigger_code": trigger_code,
        "candidates": list(candidates),
        "impact": {"claims": 1},
    }


class FaultPlan:
    """One scripted failure inside the commit transaction, armed for one call."""

    def __init__(self):
        self.marker = ""
        self.remaining = 0
        self.error = None
        self.fired = 0

    def arm(self, marker, *, occurrence=1):
        self.marker = marker
        self.remaining = occurrence
        self.error = ClaimStoreError(f"Injected failure at {marker!r}.")
        self.fired = 0

    def check(self, sql):
        if self.remaining <= 0 or self.marker not in sql:
            return
        self.remaining -= 1
        if self.remaining == 0:
            self.fired += 1
            raise self.error


class ScriptedConnection:
    """The connection `commit_changes` writes through, with one statement shape able to fail."""

    def __init__(self, connection, plan):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_plan", plan)

    def execute(self, sql, parameters=()):
        self._plan.check(sql)
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class FaultingClaimStore(ClaimStore):
    """ClaimStore with a scriptable failure at one write point of the commit."""

    def __init__(self, database, *, knowledge_space_id=SPACE):
        self.plan = FaultPlan()
        super().__init__(database, knowledge_space_id=knowledge_space_id)

    @contextmanager
    def _db(self):
        with super()._db() as connection:
            yield ScriptedConnection(connection, self.plan)

    def fail_on(self, marker, *, occurrence=1):
        self.plan.arm(marker, occurrence=occurrence)


class ClaimStoreTestCase(unittest.TestCase):
    """A real store on a real file, plus the helpers the cases below need."""

    def setUp(self):
        self.fresh_store()

    def fresh_store(self):
        """A new database and store, for a case that needs more than one attempt."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "knowledge.sqlite3"
        self.store = self.make_store(self.database)
        self.alpha = Scope.of(SPACE, "alpha")
        self.beta = Scope.of(SPACE, "beta")
        return self.store

    def make_store(self, database):
        return ClaimStore(database, knowledge_space_id=SPACE)

    def add_source(self, scope, *, tag="1", text=None, captured_at=None, source_type="file"):
        """Freeze one revision, store its artifact, register one citation over the whole text.

        Returns the revision, the artifact, the evidence id and the saved text, which
        is what both the claim helpers and the withdrawal cases need.
        """

        text = text if text is not None else f"Material {tag} for {scope.project_id}."
        revision = self.store.freeze_revision(
            scope=scope,
            source_id="src_" + tag * 32,
            source_type=source_type,
            label=f"material-{tag}",
            raw_content=text,
            captured_at=captured_at,
        )
        artifact = freeze_artifact(
            revision_id=revision["revision_id"],
            text=text,
            parser_name="plain_text",
            parser_version="1",
            config_hash="sha256:" + "c" * 64,
        )
        self.store.store_artifact(scope=scope, artifact=artifact)
        record = make_evidence(
            project_id=scope.project_id,
            artifact=artifact,
            spans=[(0, len(artifact.normalized_text))],
            label=f"material-{tag}",
        )
        self.store.register_evidence(record, scope=scope)
        return {
            "revision": revision,
            "artifact": artifact,
            "evidence_id": record.evidence_id,
            "text": artifact.normalized_text,
        }

    def ensure_run(self, scope, run_id, *, config_fingerprint="cfg-test"):
        """Open the ingest run a commit belongs to, once.

        The store refuses to fold a run into its own commit, so a test opens it the
        way the service does before any review or drop goes in.
        """

        try:
            self.store.run_status(run_id)
        except ClaimStoreError:
            self.store.create_run(
                scope=scope,
                run_id=run_id,
                config_fingerprint=config_fingerprint,
                coverage={"batches": [{"batch_id": "batch-1", "source_ids": []}]},
            )

    def commit(
        self,
        key,
        claims=(),
        *,
        scope=None,
        base_version=None,
        relations=(),
        topics=(),
        reviews=(),
        dropped=(),
        run_id="run-1",
        actor="actor-alice",
    ):
        """Commit one change set against a project, with its run opened first."""

        scope = scope or self.alpha
        if base_version is None:
            base_version = self.store.current_version(scope.project_id)
        self.ensure_run(scope, run_id)
        changeset = build_change_set(
            knowledge_space_id=scope.knowledge_space_id,
            project_id=scope.project_id,
            run_id=run_id,
            claims=list(claims),
            relations=list(relations),
            topics=list(topics),
            reviews=list(reviews),
            dropped=list(dropped),
            base_version=base_version,
        )
        return self.store.commit_changes(
            actor_subject=actor,
            base_version=base_version,
            idempotency_key=key,
            changeset=changeset,
            run_id=run_id,
            project_id=scope.project_id,
        )

    def commit_changeset(self, changeset, *, key, base_version, actor="actor-alice", run_id="run-1", scope=None):
        scope = scope or self.alpha
        self.ensure_run(scope, run_id)
        return self.store.commit_changes(
            actor_subject=actor,
            base_version=base_version,
            idempotency_key=key,
            changeset=changeset,
            run_id=run_id,
            project_id=scope.project_id,
        )

    def items(self, table, *, where="", parameters=()):
        """Read the database file directly, the way an auditor checks a rollback."""

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            query = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
            return [dict(row) for row in connection.execute(query, parameters)]
        finally:
            connection.close()

    def table_counts(self, *tables):
        return {table: len(self.items(table)) for table in tables}

    def statements(self, scope=None, *, history=False):
        scope = scope or self.alpha
        return [row["statement"] for row in self.store.iter_claims(scope, include_history=history)]

    def version_of(self, claim_id, scope=None):
        scope = scope or self.alpha
        return self.store.get_claim(claim_id, scope)["current_version_id"]


class ScopeAndStateTests(ClaimStoreTestCase):
    @case("M01")
    def test_M01_scope_references_cannot_cross_a_project_boundary(self):
        alpha_material = self.add_source(self.alpha, tag="a")
        beta_material = self.add_source(self.beta, tag="b")
        version_before = self.store.current_version("alpha")
        claims_before = len(self.items("claims"))

        with self.assertRaises(ScopeError):
            self.commit(
                "m01-foreign-evidence",
                [claim("An alpha claim citing beta evidence.", evidence=[beta_material["evidence_id"]])],
                base_version=0,
            )
        self.assertEqual(version_before, self.store.current_version("alpha"))
        self.assertEqual(claims_before, len(self.items("claims")))
        self.assertEqual([], self.items("claims", where="project_id = ?", parameters=("alpha",)))

        foreign_record = make_evidence(
            project_id=self.alpha.project_id, artifact=beta_material["artifact"], spans=[(0, 5)]
        )
        with self.assertRaises(ScopeError):
            self.store.register_evidence(foreign_record, scope=self.alpha)
        self.assertEqual(
            [],
            self.items("evidence_refs", where="evidence_id = ?", parameters=(foreign_record.evidence_id,)),
        )

        alpha_claim = self.commit(
            "m01-alpha", [claim("Alpha harness judgment.", evidence=[alpha_material["evidence_id"]])], base_version=0
        )
        beta_claim = self.commit(
            "m01-beta",
            [claim("Beta harness judgment.", evidence=[beta_material["evidence_id"]])],
            base_version=0,
            scope=self.beta,
        )
        alpha_version = self.version_of(alpha_claim.created_claim_ids[0])
        beta_version = self.version_of(beta_claim.created_claim_ids[0], self.beta)
        version_after_claims = self.store.current_version("alpha")

        with self.assertRaises(ScopeError):
            self.commit(
                "m01-cross-relation",
                [],
                base_version=version_after_claims,
                relations=[edge("supports", alpha_version, beta_version)],
            )
        self.assertEqual([], self.store.relations_of_type("supports", self.alpha))
        self.assertEqual(version_after_claims, self.store.current_version("alpha"))

        wrong_project = build_change_set(
            knowledge_space_id=SPACE, project_id="beta", run_id="run-1", claims=[], base_version=version_after_claims
        )
        with self.assertRaises(ScopeError):
            self.commit_changeset(wrong_project, key="m01-wrong-project", base_version=version_after_claims)
        wrong_space = build_change_set(
            knowledge_space_id="another-space",
            project_id="alpha",
            run_id="run-1",
            claims=[],
            base_version=version_after_claims,
        )
        with self.assertRaises(ScopeError):
            self.commit_changeset(wrong_space, key="m01-wrong-space", base_version=version_after_claims)
        self.assertEqual(version_after_claims, self.store.current_version("alpha"))
        self.assertEqual([], self.items("knowledge_submissions", where="idempotency_key = ?", parameters=("m01-wrong-project",)))
        self.assertEqual([], self.items("knowledge_submissions", where="idempotency_key = ?", parameters=("m01-wrong-space",)))

        topic = self.store.ensure_topic(scope=self.alpha, canonical_label="Agent Harness", aliases=["harness"])
        linked_alpha = self.commit(
            "m01-topic-alpha",
            [claim("Alpha topic claim.", evidence=[alpha_material["evidence_id"]], topic_ids=[topic])],
            base_version=self.store.current_version("alpha"),
        )
        linked_beta = self.commit(
            "m01-topic-beta",
            [claim("Beta topic claim.", evidence=[beta_material["evidence_id"]], topic_ids=[topic])],
            base_version=self.store.current_version("beta"),
            scope=self.beta,
        )
        self.assertEqual(list(linked_alpha.created_claim_ids), self.store.claim_ids_for_topic(topic, self.alpha))
        self.assertEqual(list(linked_beta.created_claim_ids), self.store.claim_ids_for_topic(topic, self.beta))
        self.assertEqual([topic], [row["topic_id"] for row in self.store.topic_candidates(scope=self.beta, label="harness")])
        self.assertEqual({"Alpha harness judgment.", "Alpha topic claim."}, set(self.statements()))
        self.assertEqual({"Beta harness judgment.", "Beta topic claim."}, set(self.statements(self.beta)))
        with self.assertRaises(ScopeError):
            self.store.get_claim(linked_beta.created_claim_ids[0], self.alpha)
        with self.assertRaises(EvidenceError) as error:
            self.store.load_evidence(beta_material["evidence_id"], self.alpha)
        self.assertEqual("SCOPE_MISMATCH", error.exception.code)

    @case("M02")
    def test_M02_legal_state_axes_are_stored_and_unsupported_ones_are_refused(self):
        material = self.add_source(self.alpha, tag="a")
        evidence_id = material["evidence_id"]

        explicit_hypothesis = self.commit(
            "m02-hypothesis",
            [claim("The harness may own deployment.", evidence=[evidence_id], epistemic_status="hypothesis")],
            base_version=0,
        )
        version = self.store.get_claim(explicit_hypothesis.created_claim_ids[0], self.alpha)["selected_version"]
        self.assertEqual("explicit", version["derivation"])
        self.assertEqual("hypothesis", version["epistemic_status"])

        proposed = self.commit(
            "m02-proposed",
            [claim("Adopt Terraform.", evidence=[evidence_id], kind="decision", decision_state="proposed")],
            base_version=1,
        )
        self.assertEqual(
            "proposed",
            self.store.get_claim(proposed.created_claim_ids[0], self.alpha)["selected_version"]["decision_state"],
        )

        version_before = self.store.current_version("alpha")
        with self.assertRaises(KnowledgeError) as error:
            self.commit(
                "m02-adopted-unsupported",
                [claim("Adopt Pulumi.", evidence=[evidence_id], kind="decision", decision_state="adopted")],
                base_version=version_before,
            )
        self.assertEqual("MISSING_SUPPORT", error.exception.code)
        self.assertEqual(version_before, self.store.current_version("alpha"))
        self.assertNotIn("Adopt Pulumi.", self.statements(history=True))

        adoption = self.commit(
            "m02-adopted",
            [
                claim(
                    "Adopt Pulumi.",
                    evidence=[evidence_id],
                    kind="decision",
                    decision_state="adopted",
                    support=["adoption_record"],
                )
            ],
            base_version=version_before,
        )
        self.assertEqual(
            "adopted",
            self.store.get_claim(adoption.created_claim_ids[0], self.alpha)["selected_version"]["decision_state"],
        )

        with self.assertRaises(KnowledgeError) as error:
            self.commit(
                "m02-verified-unsupported",
                [claim("The harness is verified.", evidence=[evidence_id], kind="fact", epistemic_status="verified")],
                base_version=self.store.current_version("alpha"),
            )
        self.assertEqual("MISSING_SUPPORT", error.exception.code)
        self.assertNotIn("The harness is verified.", self.statements(history=True))

        definition = self.commit(
            "m02-definition",
            [claim("A harness is a runner.", evidence=[evidence_id], kind="definition", epistemic_status="not_applicable")],
            base_version=self.store.current_version("alpha"),
        )
        self.assertEqual(
            "not_applicable",
            self.store.get_claim(definition.created_claim_ids[0], self.alpha)["selected_version"]["epistemic_status"],
        )

        adopted = ClaimState(
            knowledge_kind="decision", derivation="explicit", epistemic_status="asserted", decision_state="adopted"
        )
        self.assertEqual(["adoption_record"], adopted.required_support())
        with self.assertRaises(KnowledgeError) as error:
            validate_claim_state(adopted, [])
        self.assertEqual("MISSING_SUPPORT", error.exception.code)
        self.assertIs(adopted, validate_claim_state(adopted, ["adoption_record"]))
        with self.assertRaises(KnowledgeError) as error:
            validate_claim_state(ClaimState(knowledge_kind="fact", derivation="explicit", epistemic_status="verified"), [])
        self.assertEqual("MISSING_SUPPORT", error.exception.code)
        self.assertEqual(
            ["adoption_record"],
            ClaimState(
                knowledge_kind="decision",
                derivation="explicit",
                epistemic_status="asserted",
                decision_state="adopted",
            ).required_support(),
        )
        with self.assertRaises(KnowledgeError) as error:
            ClaimState(knowledge_kind="fact", derivation="explicit", epistemic_status="asserted", decision_state="adopted")
        self.assertEqual("INVALID_STATE", error.exception.code)
        with self.assertRaises(KnowledgeError) as error:
            ClaimState(knowledge_kind="fact", derivation="explicit", epistemic_status="asserted", question_state="open")
        self.assertEqual("INVALID_STATE", error.exception.code)

    @case("M03")
    def test_M03_the_store_mints_identity_and_records_the_authenticated_actor(self):
        material = self.add_source(self.alpha, tag="a")
        evidence_id = material["evidence_id"]
        forged_version = "clv_" + "f" * 32
        forged_claim = "clm_" + "e" * 32

        committed = self.commit(
            "m03-forged-version",
            [dict(claim("The harness owns deployment.", evidence=[evidence_id]), claim_version_id=forged_version)],
            base_version=0,
        )
        claim_id = committed.created_claim_ids[0]
        stored = self.store.get_claim(claim_id, self.alpha)
        self.assertRegex(claim_id, CLAIM_ID_PATTERN)
        self.assertRegex(stored["current_version_id"], CLAIM_VERSION_PATTERN)
        self.assertNotEqual(forged_version, stored["current_version_id"])
        self.assertNotIn(forged_version, [row["claim_version_id"] for row in self.items("claim_versions")])
        self.assertNotIn(forged_claim, [row["claim_id"] for row in self.items("claims")])
        for row in self.items("claim_versions"):
            self.assertRegex(row["claim_version_id"], CLAIM_VERSION_PATTERN)

        with self.assertRaises(ScopeError):
            self.commit(
                "m03-forged-claim",
                [claim("A claim with a foreign id.", evidence=[evidence_id], claim_id=forged_claim)],
                base_version=1,
            )
        with self.assertRaises(ClaimStoreError):
            self.commit(
                "m03-malformed-claim",
                [claim("A claim with a malformed id.", evidence=[evidence_id], claim_id="clm_not-a-uuid")],
                base_version=1,
            )
        self.assertEqual([], self.items("claims", where="claim_id = ?", parameters=(forged_claim,)))
        self.assertEqual(["The harness owns deployment."], self.statements())

        actor_result = self.commit(
            "m03-actor",
            [dict(claim("A second proposition.", evidence=[evidence_id]), actor_subject="mallory")],
            base_version=1,
            actor="actor-alice",
        )
        version_row = self.store.get_claim(actor_result.created_claim_ids[0], self.alpha)["selected_version"]
        self.assertEqual("actor-alice", version_row["actor_subject"])
        self.assertEqual("standup", version_row["asserted_by"])
        self.assertEqual([], [row for row in self.items("claim_versions") if row["actor_subject"] != "actor-alice"])

        stale_schema = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("A claim under a stale schema.", evidence=[evidence_id])],
            base_version=2,
        )
        stale_schema["schema_version"] = 1
        with self.assertRaises(ClaimStoreError):
            self.commit_changeset(stale_schema, key="m03-schema", base_version=2)
        self.assertEqual(2, self.store.current_version("alpha"))
        self.assertNotIn("A claim under a stale schema.", self.statements(history=True))
        self.assertEqual([], self.items("knowledge_submissions", where="idempotency_key = ?", parameters=("m03-schema",)))

    @case("M03")
    def test_M03_a_change_set_with_an_unknown_top_level_shape_is_refused(self):
        material = self.add_source(self.alpha, tag="a")
        version_before = self.store.current_version("alpha")

        with self.assertRaises(ClaimStoreError):
            self.store.commit_changes(
                actor_subject="actor-alice",
                base_version=version_before,
                idempotency_key="m03-not-an-object",
                changeset=[{"statement": "A list is not a change set."}],
                project_id="alpha",
            )

        changeset = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("A claim carried beside an unknown shape.", evidence=[material["evidence_id"]])],
            base_version=version_before,
        )
        changeset["pages"] = [{"slug": "legacy", "body": "a v1 update package"}]
        with self.assertRaises(ClaimStoreError):
            self.commit_changeset(changeset, key="m03-unknown-shape", base_version=version_before)

        self.assertEqual(version_before, self.store.current_version("alpha"))
        self.assertEqual([], self.items("claims"))
        self.assertEqual([], self.items("knowledge_submissions", where="idempotency_key = ?", parameters=("m03-unknown-shape",)))


class TopicIdentityTests(ClaimStoreTestCase):
    @case("R01")
    def test_R01_two_projects_share_one_topic_identity_but_never_their_claims(self):
        alpha_material = self.add_source(self.alpha, tag="a")
        beta_material = self.add_source(self.beta, tag="b")
        topic = self.store.ensure_topic(scope=self.alpha, canonical_label="Agent Harness", aliases=["harness"])

        alpha_result = self.commit(
            "r01-alpha",
            [claim("The harness owns deployment in alpha.", evidence=[alpha_material["evidence_id"]], topic_ids=[topic])],
            base_version=0,
        )
        beta_result = self.commit(
            "r01-beta",
            [claim("The harness is only a test helper in beta.", evidence=[beta_material["evidence_id"]], topic_ids=[topic])],
            base_version=0,
            scope=self.beta,
        )

        alpha_id = alpha_result.created_claim_ids[0]
        beta_id = beta_result.created_claim_ids[0]
        self.assertEqual([topic], self.store.get_claim(alpha_id, self.alpha)["topic_ids"])
        self.assertEqual([topic], self.store.get_claim(beta_id, self.beta)["topic_ids"])
        self.assertEqual([alpha_id], self.store.claim_ids_for_topic(topic, self.alpha))
        self.assertEqual([beta_id], self.store.claim_ids_for_topic(topic, self.beta))
        self.assertEqual([topic], [row["topic_id"] for row in self.store.topic_candidates(scope=self.alpha, label="Agent Harness")])
        self.assertEqual([topic], [row["topic_id"] for row in self.store.topic_candidates(scope=self.beta, label="harness")])

        self.assertEqual(["The harness owns deployment in alpha."], self.statements())
        self.assertEqual(["The harness is only a test helper in beta."], self.statements(self.beta))
        with self.assertRaises(ScopeError):
            self.store.get_claim(beta_id, self.alpha)
        with self.assertRaises(EvidenceError) as error:
            self.store.load_evidence(beta_material["evidence_id"], self.alpha)
        self.assertEqual("SCOPE_MISMATCH", error.exception.code)

    @case("R02")
    def test_R02_one_alias_may_mean_two_topics_until_an_explicit_merge(self):
        material = self.add_source(self.alpha, tag="a")
        first = self.store.ensure_topic(scope=self.alpha, canonical_label="Harness", aliases=["harness"])
        second = self.store.ensure_topic(scope=self.alpha, canonical_label="Harness Platform", aliases=["harness"])
        self.assertNotEqual(first, second)
        candidates = self.store.topic_candidates(scope=self.alpha, label="harness")
        self.assertEqual({first, second}, {candidate["topic_id"] for candidate in candidates})
        self.assertEqual([], self.store.topic_merge_log(self.alpha))

        created = self.commit(
            "r02", [claim("The harness owns deployment.", evidence=[material["evidence_id"]], topic_ids=[second])], base_version=0
        )
        claim_id = created.created_claim_ids[0]

        moved = self.store.merge_topics(
            scope=self.alpha, source_topic_id=second, target_topic_id=first, actor_subject="actor-bob"
        )
        self.assertEqual(
            {"source_topic_id": second, "target_topic_id": first, "moved_claims": 1, "actor_subject": "actor-bob"},
            moved,
        )
        self.assertEqual([claim_id], self.store.claim_ids_for_topic(first, self.alpha))
        self.assertEqual([claim_id], self.store.claim_ids_for_topic(second, self.alpha))
        self.assertEqual(
            [{"topic_id": second, "canonical_label": "Harness Platform", "merged_into_topic_id": first}],
            self.store.topic_merge_log(self.alpha),
        )
        retired = self.items("topics", where="topic_id = ?", parameters=(second,))
        self.assertEqual(1, len(retired))
        self.assertEqual("merged", retired[0]["status"])
        self.assertEqual(first, retired[0]["merged_into_topic_id"])
        self.assertEqual([first], [row["topic_id"] for row in self.store.topic_candidates(scope=self.alpha, label="harness")])


class ClaimEvolutionTests(ClaimStoreTestCase):
    @case("R03")
    def test_R03_a_synthesized_claim_keeps_its_origin_when_a_user_says_it_outright(self):
        first = self.add_source(self.alpha, tag="1")
        second = self.add_source(self.alpha, tag="2")
        synthesized_origin = {
            "derivation": "synthesized",
            "evidence_refs": [first["evidence_id"]],
            "inference_note": "Read out of the first material.",
        }

        created = self.commit(
            "r03-v1",
            [
                claim(
                    "The harness owns deployment.",
                    evidence=[first["evidence_id"]],
                    derivation="synthesized",
                    origins=[synthesized_origin],
                )
            ],
            base_version=0,
        )
        claim_id = created.created_claim_ids[0]
        explicit_origin = {"derivation": "explicit", "evidence_refs": [second["evidence_id"]]}
        updated = self.commit(
            "r03-v2",
            [
                claim(
                    "The harness owns deployment.",
                    evidence=[second["evidence_id"]],
                    origins=[explicit_origin],
                )
            ],
            base_version=1,
        )
        self.assertEqual([claim_id], list(updated.updated_claim_ids))
        self.assertEqual((), updated.created_claim_ids)

        stored = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual(claim_id, stored["claim_id"])
        self.assertEqual(2, len(stored["origins"]))
        self.assertEqual(
            {("synthesized", first["evidence_id"]), ("explicit", second["evidence_id"])},
            {
                (origin["derivation"], evidence_id)
                for origin in stored["origins"]
                for evidence_id in origin["evidence_refs"]
            },
        )
        self.assertEqual([1, 2], [row["version"] for row in stored["history"]])

        version_one = self.store.get_claim(claim_id, self.alpha, version=1)
        self.assertEqual("synthesized", version_one["selected_version"]["derivation"])
        self.assertEqual(
            [("synthesized", first["evidence_id"])],
            [(origin["derivation"], origin["evidence_refs"][0]) for origin in version_one["origins"]],
        )
        self.assertEqual(1, len(self.store.iter_claims(self.alpha)))
        self.assertEqual(2, len(self.store.iter_claims(self.alpha, include_history=True)))

    @case("R04")
    def test_R04_older_material_imported_later_does_not_reorder_the_decision_in_force(self):
        """A record captured later but asserted earlier is stored in commit order.

        The decision already on file keeps its own version, its own stated time and
        its own state, and the late capture supersedes nothing.
        """

        in_force = self.add_source(
            self.alpha,
            tag="1",
            text="Decision of 10 September: adopt Postgres for the ledger.",
            captured_at="2026-09-11T00:00:00Z",
        )
        older = self.add_source(
            self.alpha,
            tag="2",
            text="Minutes of 1 August: adopt Postgres for the ledger.",
            captured_at="2026-09-16T00:00:00Z",
        )
        decision = claim(
            "Adopt Postgres for the ledger.",
            evidence=[in_force["evidence_id"]],
            kind="decision",
            decision_state="adopted",
            support=["adoption_record"],
        )
        created = self.commit("r04-new", [decision], base_version=0)
        claim_id = created.created_claim_ids[0]
        before = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual("2026-09-10", before["selected_version"]["asserted_at"])

        older_minutes = claim(
            "Adopt Postgres for the ledger.",
            evidence=[older["evidence_id"]],
            kind="decision",
            decision_state="adopted",
            support=["adoption_record"],
            attribution={"asserted_by": "minutes", "asserted_at": "2026-08-01", "asserted_at_precision": "day"},
        )
        self.commit("r04-old", [older_minutes], base_version=1)

        recorded = self.store.get_claim(claim_id, self.alpha, version=1)
        self.assertEqual(before["selected_version"]["claim_version_id"], recorded["selected_version"]["claim_version_id"])
        self.assertEqual("2026-09-10", recorded["selected_version"]["asserted_at"])
        self.assertEqual("adopted", recorded["selected_version"]["decision_state"])
        self.assertEqual("active", recorded["selected_version"]["lifecycle_status"])

        current = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual(before["selected_version"]["statement"], current["selected_version"]["statement"])
        self.assertEqual("adopted", current["selected_version"]["decision_state"])
        self.assertEqual("active", current["selected_version"]["lifecycle_status"])
        self.assertEqual([1, 2], [row["version"] for row in current["history"]])
        history = self.store.iter_claims(self.alpha, include_history=True)
        self.assertEqual([1, 2], [row["knowledge_version"] for row in history])
        self.assertEqual({"2026-09-10", "2026-08-01"}, {row["asserted_at"] for row in history})
        self.assertEqual(
            "2026-08-01", self.store.get_claim(claim_id, self.alpha, version=2)["selected_version"]["asserted_at"]
        )
        self.assertEqual([], self.store.relations_of_type("supersedes", self.alpha))

        separately_worded = claim(
            "Keep the existing ledger engine instead of adopting a new one.",
            evidence=[older["evidence_id"]],
            kind="decision",
            decision_state="proposed",
            attribution={"asserted_by": "minutes", "asserted_at": "2026-08-01", "asserted_at_precision": "day"},
        )
        older_claim = self.commit("r04-old-wording", [separately_worded], base_version=2)
        older_id = older_claim.created_claim_ids[0]
        current_versions = {row["claim_id"]: row["claim_version_id"] for row in self.store.iter_claims(self.alpha)}
        self.assertIn(claim_id, current_versions)
        self.assertEqual("2026-08-01", self.store.get_claim(older_id, self.alpha)["selected_version"]["asserted_at"])
        self.assertEqual("active", self.store.get_claim(claim_id, self.alpha)["selected_version"]["lifecycle_status"])
        self.assertEqual([], self.store.relations_of_type("supersedes", self.alpha))
        historical = {
            (row["claim_id"], row["version"], row["asserted_at"])
            for row in self.store.iter_claims(self.alpha, include_history=True)
        }
        self.assertIn((claim_id, 1, "2026-09-10"), historical)
        self.assertIn((claim_id, 2, "2026-08-01"), historical)
        self.assertIn((older_id, 1, "2026-08-01"), historical)

    @case("R05")
    def test_R05_a_narrower_statement_is_a_new_claim_and_refines_it_explicitly(self):
        material = self.add_source(self.alpha, tag="a")
        broad = self.commit(
            "r05-broad", [claim("The harness owns deployment.", evidence=[material["evidence_id"]])], base_version=0
        )
        broad_id = broad.created_claim_ids[0]

        narrow = self.commit(
            "r05-narrow",
            [
                claim(
                    "The harness owns deployment only on the staging cluster.",
                    evidence=[material["evidence_id"]],
                    conditions=["staging cluster only"],
                )
            ],
            base_version=1,
        )
        self.assertEqual(1, narrow.created_claims)
        narrow_id = narrow.created_claim_ids[0]
        self.assertNotEqual(broad_id, narrow_id)
        self.assertNotEqual(
            self.store.get_claim(broad_id, self.alpha)["fingerprint"],
            self.store.get_claim(narrow_id, self.alpha)["fingerprint"],
        )
        self.assertEqual([], self.store.relations_of_type("supersedes", self.alpha))
        self.assertEqual("active", self.store.get_claim(broad_id, self.alpha)["selected_version"]["lifecycle_status"])
        self.assertEqual("active", self.store.get_claim(narrow_id, self.alpha)["selected_version"]["lifecycle_status"])

        narrow_version = self.version_of(narrow_id)
        broad_version = self.version_of(broad_id)
        refined = self.commit(
            "r05-refines",
            [],
            base_version=2,
            relations=[edge("refines", narrow_version, broad_version)],
        )
        self.assertEqual(1, refined.new_relations)
        self.assertEqual(
            [(narrow_version, broad_version)],
            [
                (row["from_claim_version_id"], row["to_claim_version_id"])
                for row in self.store.relations_of_type("refines", self.alpha)
            ],
        )
        self.assertEqual("active", self.store.get_claim(broad_id, self.alpha)["selected_version"]["lifecycle_status"])
        self.assertEqual("active", self.store.get_claim(narrow_id, self.alpha)["selected_version"]["lifecycle_status"])
        self.assertEqual(2, len(self.store.iter_claims(self.alpha)))

    @case("R06")
    def test_R06_contradictory_judgments_are_both_kept_and_recorded_once(self):
        material = self.add_source(self.alpha, tag="a")
        both = self.commit(
            "r06",
            [
                claim("The harness is the deployment tool.", evidence=[material["evidence_id"]]),
                claim("The harness is only a test helper.", evidence=[material["evidence_id"]]),
            ],
            base_version=0,
        )
        first_id, second_id = both.created_claim_ids
        self.assertEqual(2, both.created_claims)
        self.assertEqual(
            ["active", "active"],
            [self.store.get_claim(claim_id, self.alpha)["selected_version"]["lifecycle_status"] for claim_id in (first_id, second_id)],
        )
        first_version = self.version_of(first_id)
        second_version = self.version_of(second_id)

        forwards = self.commit(
            "r06-a",
            [],
            base_version=1,
            relations=[edge("contradicts", first_version, second_version)],
        )
        backwards = self.commit(
            "r06-b",
            [],
            base_version=2,
            relations=[edge("contradicts", second_version, first_version)],
        )
        self.assertEqual(1, forwards.new_relations)
        self.assertEqual(0, backwards.new_relations)
        stored = self.store.relations_of_type("contradicts", self.alpha)
        self.assertEqual(1, len(stored))
        self.assertEqual(sorted((first_version, second_version)), sorted((stored[0]["from_claim_version_id"], stored[0]["to_claim_version_id"])))
        for claim_id in (first_id, second_id):
            version = self.store.get_claim(claim_id, self.alpha)["selected_version"]
            self.assertEqual("active", version["lifecycle_status"])
            self.assertEqual("asserted", version["epistemic_status"])
            self.assertIn(version["statement"], {"The harness is the deployment tool.", "The harness is only a test helper."})
        self.assertEqual([], self.store.relations_of_type("supersedes", self.alpha))

    @case("R07")
    def test_R07_a_withdrawn_premise_is_recomputed_across_support_groups(self):
        premise_a = self.add_source(self.alpha, tag="a", text="Premise A: the harness runs the deployment.")
        premise_b = self.add_source(self.alpha, tag="b", text="Premise B: the deploy job is owned by the harness.")
        independent = self.add_source(self.alpha, tag="d", text="Premise D: the deploy runbook names the harness.")
        conjunction = {
            "derivation": "synthesized",
            "evidence_refs": [premise_a["evidence_id"], premise_b["evidence_id"]],
            "inference_note": "A and B together entail the claim.",
        }
        independent_origin = {"derivation": "explicit", "evidence_refs": [independent["evidence_id"]]}

        created = self.commit(
            "r07",
            [
                claim(
                    "The harness owns deployment.",
                    evidence=[premise_a["evidence_id"], premise_b["evidence_id"], independent["evidence_id"]],
                    derivation="synthesized",
                    origins=[conjunction, independent_origin],
                )
            ],
            base_version=0,
        )
        claim_id = created.created_claim_ids[0]
        self.assertEqual("grounded", self.store.get_claim(claim_id, self.alpha)["selected_version"]["grounding_status"])
        self.assertEqual("grounded", support_group_outcome([[True, True], [True]]))
        self.assertEqual("needs_revalidation", support_group_outcome([[False, True], [False]]))
        self.assertEqual("unsupported", support_group_outcome([[False, False], [False]]))

        self.store.withdraw_source("src_" + "a" * 32, self.alpha)
        withdrawn = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual("grounded", withdrawn["selected_version"]["grounding_status"])
        self.assertEqual("active", withdrawn["selected_version"]["lifecycle_status"])
        self.assertNotEqual("retracted", withdrawn["selected_version"]["lifecycle_status"])
        self.assertEqual([1], [row["version"] for row in withdrawn["history"]])
        self.assertEqual([("synthesized", premise_a["evidence_id"], False)], [
            (origin["derivation"], requirement["id"], requirement["available"])
            for origin in withdrawn["origins"]
            for requirement in origin["support_requirements"]
            if requirement["id"] == premise_a["evidence_id"]
        ])

        self.store.withdraw_source("src_" + "d" * 32, self.alpha)
        partly = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual("needs_revalidation", partly["selected_version"]["grounding_status"])
        self.assertEqual("active", partly["selected_version"]["lifecycle_status"])

        self.store.withdraw_source("src_" + "b" * 32, self.alpha)
        unsupported = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual("unsupported", unsupported["selected_version"]["grounding_status"])
        self.assertEqual("active", unsupported["selected_version"]["lifecycle_status"])
        self.assertEqual("asserted", unsupported["selected_version"]["epistemic_status"])
        self.assertEqual([1], [row["version"] for row in unsupported["history"]])
        self.assertEqual(1, len(self.store.iter_claims(self.alpha)))
        with self.assertRaises(EvidenceError) as error:
            self.store.load_evidence(premise_a["evidence_id"], self.alpha)
        self.assertEqual("SOURCE_WITHDRAWN", error.exception.code)

    @case("R08")
    def test_R08_illegal_derivation_shapes_are_refused_and_a_valid_chain_is_accepted(self):
        material = self.add_source(self.alpha, tag="a")
        beta_material = self.add_source(self.beta, tag="b")
        seed = self.commit(
            "r08-seed",
            [
                claim("Claim A.", evidence=[material["evidence_id"]]),
                claim("Claim B.", evidence=[material["evidence_id"]]),
            ],
            base_version=0,
        )
        claim_a, claim_b = seed.created_claim_ids
        version_a = self.version_of(claim_a)
        version_b = self.version_of(claim_b)

        chain = self.commit("r08-chain", [], base_version=1, relations=[edge("derived_from", version_b, version_a)])
        self.assertEqual(1, chain.new_relations)
        derived = self.commit(
            "r08-derived",
            [
                claim(
                    "Claim C follows from B.",
                    evidence=[material["evidence_id"]],
                    derivation="synthesized",
                    origins=[
                        {
                            "derivation": "synthesized",
                            "evidence_refs": [material["evidence_id"]],
                            "inference_note": "B, read as a premise.",
                            "premise_claim_version_ids": [version_b],
                        }
                    ],
                )
            ],
            base_version=2,
        )
        derived_id = derived.created_claim_ids[0]
        self.assertEqual(
            [version_b],
            [
                requirement["id"]
                for origin in self.store.get_claim(derived_id, self.alpha)["origins"]
                for requirement in origin["support_requirements"]
                if requirement["kind"] == "claim_version"
            ],
        )
        version_now = self.store.current_version("alpha")

        with self.assertRaises(ClaimStoreError):
            self.commit(
                "r08-cycle",
                [],
                base_version=version_now,
                relations=[edge("derived_from", version_a, version_b)],
            )
        with self.assertRaises(ClaimStoreError):
            self.commit(
                "r08-self",
                [],
                base_version=version_now,
                relations=[edge("supports", version_a, version_a)],
            )
        with self.assertRaises(ClaimStoreError):
            self.commit(
                "r08-missing-endpoint",
                [],
                base_version=version_now,
                relations=[edge("supports", version_a, "clv_" + "f" * 32)],
            )
        beta_claim = self.commit(
            "r08-beta", [claim("Beta claim.", evidence=[beta_material["evidence_id"]])], base_version=0, scope=self.beta
        )
        with self.assertRaises(ScopeError):
            self.commit(
                "r08-cross-project",
                [],
                base_version=version_now,
                relations=[edge("supports", version_a, self.version_of(beta_claim.created_claim_ids[0], self.beta))],
            )

        self.assertEqual(1, len(self.store.relations_of_type("derived_from", self.alpha)))
        self.assertEqual([], self.store.relations_of_type("supports", self.alpha))
        self.assertEqual(version_now, self.store.current_version("alpha"))
        self.assertEqual(["a", "b", "a"], find_derivation_cycle(["a", "b"], [("a", "b"), ("b", "a")]))
        self.assertIsNone(find_derivation_cycle(["a", "b", "c"], [("c", "b"), ("b", "a")]))
        with self.assertRaises(KnowledgeError) as error:
            ClaimRelation(
                relation_id="rel_" + "1" * 32,
                relation_type="supports",
                from_claim_version_id=version_a,
                to_claim_version_id=version_a,
            )
        self.assertEqual("INVALID_RELATION", error.exception.code)

        # A Topic to Claim edge is the other half of the case. It is refused before
        # a relation object exists at all, because both endpoints are typed claim
        # versions, so a topic id cannot be spelled into one of those slots.
        for endpoint in ("top_" + "2" * 32, "topic:harness", "claim-1"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(KnowledgeError) as mixed:
                    ClaimRelation(
                        relation_id="rel_" + "3" * 32,
                        relation_type="supports",
                        from_claim_version_id=version_a,
                        to_claim_version_id=endpoint,
                    )
                self.assertEqual("INVALID_ID", mixed.exception.code)
        with self.assertRaises(ClaimStoreError):
            self.commit(
                "r08-topic-endpoint",
                [],
                base_version=version_now,
                relations=[edge("supports", version_a, "top_" + "4" * 32)],
            )
        self.assertEqual([], self.store.relations_of_type("supports", self.alpha))


class ReviewActionTests(ClaimStoreTestCase):
    @case("V01")
    def test_V01_retain_records_retention_without_verifying_adopting_or_making_explicit(self):
        material = self.add_source(self.alpha, tag="a")
        evidence_id = material["evidence_id"]
        created = self.commit(
            "v01-claims",
            [
                claim(
                    "The harness may own deployment.",
                    evidence=[evidence_id],
                    derivation="synthesized",
                    epistemic_status="hypothesis",
                    origins=[
                        {
                            "derivation": "synthesized",
                            "evidence_refs": [evidence_id],
                            "inference_note": "Read as a possibility.",
                        }
                    ],
                ),
                claim("Adopt Postgres for the ledger.", evidence=[evidence_id], kind="decision", decision_state="proposed"),
                claim(
                    "Deployment and testing share one harness.",
                    evidence=[evidence_id],
                    derivation="synthesized",
                    origins=[
                        {
                            "derivation": "synthesized",
                            "evidence_refs": [evidence_id],
                            "inference_note": "Combined reading.",
                        }
                    ],
                ),
            ],
            base_version=0,
        )
        hypothesis_id, proposal_id, synthesized_id = created.created_claim_ids
        opened_versions = {claim_id: self.version_of(claim_id) for claim_id in created.created_claim_ids}
        self.commit(
            "v01-open-reviews",
            [],
            base_version=1,
            reviews=[
                review(claim_id, opened_versions[claim_id]) for claim_id in created.created_claim_ids
            ],
        )
        before = {claim_id: self.store.get_claim(claim_id, self.alpha) for claim_id in created.created_claim_ids}

        for entry in self.store.open_reviews(self.alpha):
            outcome = self.store.review_action(
                actor_subject="actor-bob",
                review_id=entry["review_id"],
                expected_version=entry["subject_version"],
                action="retain",
                scope=self.alpha,
                idempotency_key="retain-" + entry["subject_id"],
            )
            self.assertTrue(outcome["retained"])
            self.assertFalse(outcome["epistemic_status_changed"])
        self.assertEqual([], self.store.open_reviews(self.alpha))

        after = {claim_id: self.store.get_claim(claim_id, self.alpha) for claim_id in created.created_claim_ids}
        self.assertEqual("hypothesis", after[hypothesis_id]["selected_version"]["epistemic_status"])
        self.assertEqual("proposed", after[proposal_id]["selected_version"]["decision_state"])
        self.assertEqual("synthesized", after[synthesized_id]["selected_version"]["derivation"])
        for claim_id in created.created_claim_ids:
            for axis in ("epistemic_status", "decision_state", "derivation", "lifecycle_status", "grounding_status"):
                self.assertEqual(
                    before[claim_id]["selected_version"][axis], after[claim_id]["selected_version"][axis]
                )
            self.assertEqual(before[claim_id]["current_version_id"], after[claim_id]["current_version_id"])
        # Retention is recorded on the review decision itself, which is the durable
        # audit for a review action and carries the actor alongside the outcome.
        retained_rows = [
            row for row in self.items("review_decisions") if row["action"] == "retain"
        ]
        self.assertEqual(3, len(retained_rows))
        self.assertEqual(
            [True, True, True], [json.loads(row["result_json"])["retained"] for row in retained_rows]
        )
        self.assertEqual(
            {"actor-bob"}, {row["actor_subject"] for row in retained_rows}
        )

        self.commit(
            "v01-adopt-review",
            [],
            base_version=1,
            reviews=[review(proposal_id, after[proposal_id]["current_version_id"], question="Adopt this decision?")],
        )
        self.commit(
            "v01-wrong-kind-review",
            [],
            base_version=1,
            reviews=[review(hypothesis_id, after[hypothesis_id]["current_version_id"], question="Adopt this decision?")],
        )
        by_subject = {entry["subject_id"]: entry for entry in self.store.open_reviews(self.alpha)}
        with self.assertRaises(ClaimStoreError):
            self.store.review_action(
                actor_subject="actor-bob",
                review_id=by_subject[hypothesis_id]["review_id"],
                expected_version=after[hypothesis_id]["current_version_id"],
                action="adopt_decision",
                scope=self.alpha,
                idempotency_key="adopt-hypothesis",
            )
        self.assertEqual(2, len(self.store.open_reviews(self.alpha)))

        adopted = self.store.review_action(
            actor_subject="actor-bob",
            review_id=by_subject[proposal_id]["review_id"],
            expected_version=after[proposal_id]["current_version_id"],
            action="adopt_decision",
            scope=self.alpha,
            idempotency_key="adopt-proposal",
        )
        self.assertEqual("adopted", adopted["decision_state"])
        current = self.store.get_claim(proposal_id, self.alpha)
        self.assertEqual("adopted", current["selected_version"]["decision_state"])
        self.assertNotEqual(after[proposal_id]["current_version_id"], current["current_version_id"])
        self.assertEqual([1, 2], [row["version"] for row in current["history"]])
        self.assertEqual("actor-bob", current["selected_version"]["actor_subject"])
        self.assertEqual(2, self.store.current_version("alpha"))

    @case("V02")
    def test_V02_a_review_cannot_be_applied_to_a_claim_that_moved(self):
        material = self.add_source(self.alpha, tag="a")
        created = self.commit(
            "v02-claim",
            [claim("Adopt Postgres.", evidence=[material["evidence_id"]], kind="decision", decision_state="proposed")],
            base_version=0,
        )
        claim_id = created.created_claim_ids[0]
        opened_version = self.version_of(claim_id)
        self.commit(
            "v02-open-review",
            [],
            base_version=1,
            reviews=[review(claim_id, opened_version, question="Adopt this decision?")],
        )
        review_id = self.store.open_reviews(self.alpha)[0]["review_id"]

        moved = self.commit(
            "v02-move",
            [
                claim(
                    "Adopt Postgres.",
                    evidence=[material["evidence_id"]],
                    kind="decision",
                    decision_state="proposed",
                    attribution={"asserted_by": "minutes-writer", "asserted_at": "2026-09-10", "asserted_at_precision": "day"},
                )
            ],
            base_version=1,
        )
        self.assertEqual([claim_id], list(moved.updated_claim_ids))
        moved_version = self.version_of(claim_id)
        self.assertNotEqual(opened_version, moved_version)

        with self.assertRaises(StaleReviewError) as error:
            self.store.review_action(
                actor_subject="actor-bob",
                review_id=review_id,
                expected_version=opened_version,
                action="retain",
                scope=self.alpha,
                idempotency_key="v02-stale",
            )
        self.assertEqual(moved_version, error.exception.current_version)
        self.assertEqual(1, len(self.store.open_reviews(self.alpha)))
        self.assertEqual(moved_version, self.version_of(claim_id))
        self.assertEqual([], self.items("review_decisions"))

        outcome = self.store.review_action(
            actor_subject="actor-bob",
            review_id=review_id,
            expected_version=moved_version,
            action="retain",
            scope=self.alpha,
            idempotency_key="v02-fresh",
        )
        self.assertTrue(outcome["retained"])
        self.assertEqual([], self.store.open_reviews(self.alpha))

    @case("V03")
    def test_V03_one_ambiguous_identity_holds_only_its_own_item_back(self):
        """The held item has no published version yet, so its review carries no version to compare."""

        material = self.add_source(self.alpha, tag="a")
        chosen_topic = self.store.ensure_topic(scope=self.alpha, canonical_label="Harness the deploy tool")
        other_topic = self.store.ensure_topic(scope=self.alpha, canonical_label="Harness the test fixture")
        pending_identity = "clm_" + "a" * 32
        pending_statement = "The harness owns deployment."

        result = self.commit(
            "v03",
            [
                claim("The build pipeline runs nightly.", evidence=[material["evidence_id"]]),
                claim("The deploy runbook is versioned.", evidence=[material["evidence_id"]]),
            ],
            base_version=0,
            reviews=[
                review(
                    pending_identity,
                    "",
                    trigger_code="ambiguous_identity",
                    question="Which Harness does this belong to?",
                    candidates=[chosen_topic, other_topic],
                )
            ],
        )
        self.assertEqual(2, result.created_claims)
        self.assertEqual(1, result.review_pending)
        self.assertEqual(0, result.dropped_candidates)
        self.assertEqual(2, len(self.store.iter_claims(self.alpha)))
        self.assertNotIn(pending_statement, self.statements(history=True))
        self.assertEqual([], self.items("claims", where="claim_id = ?", parameters=(pending_identity,)))
        self.assertEqual([], [row for row in self.items("stage_artifacts") if row["stage"] == "value_gate_drop"])
        opened = self.store.open_reviews(self.alpha)
        self.assertEqual(1, len(opened))
        self.assertEqual("ambiguous_identity", opened[0]["trigger_code"])
        self.assertEqual([chosen_topic, other_topic], opened[0]["candidates"])

        outcome = self.store.review_action(
            actor_subject="actor-bob",
            review_id=opened[0]["review_id"],
            expected_version="",
            action="confirm_identity",
            scope=self.alpha,
            idempotency_key="v03-confirm",
            topic_id=chosen_topic,
        )
        self.assertEqual(chosen_topic, outcome["topic_id"])
        self.assertEqual([pending_identity], self.store.claim_ids_for_topic(chosen_topic, self.alpha))
        self.assertEqual([], self.store.claim_ids_for_topic(other_topic, self.alpha))
        self.assertEqual([], self.store.open_reviews(self.alpha))


class RetryTests(ClaimStoreTestCase):
    @case("T01")
    def test_T01_a_retry_of_the_same_request_returns_the_first_outcome(self):
        material = self.add_source(self.alpha, tag="a")
        seed = self.commit(
            "t01-seed",
            [
                claim("Claim A.", evidence=[material["evidence_id"]]),
                claim("Claim B.", evidence=[material["evidence_id"]]),
            ],
            base_version=0,
        )
        version_a = self.version_of(seed.created_claim_ids[0])
        version_b = self.version_of(seed.created_claim_ids[1])
        changeset = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("Claim C.", evidence=[material["evidence_id"]])],
            relations=[edge("supports", version_a, version_b)],
            base_version=1,
        )
        first = self.commit_changeset(changeset, key="t01-key", base_version=1)
        # The retry carries the request it is retrying, base_version included, or it
        # would be a different request and the idempotency check would say so.
        retry = self.commit_changeset(changeset, key="t01-key", base_version=1)

        self.assertEqual(first, retry)
        self.assertEqual(first.as_dict(), retry.as_dict())
        self.assertEqual(2, first.knowledge_version)
        self.assertEqual(3, len(self.store.iter_claims(self.alpha)))
        self.assertEqual(3, len(self.items("claim_versions")))
        self.assertEqual(1, len(self.items("claim_relations")))
        self.assertEqual(1, len(self.items("knowledge_submissions", where="idempotency_key = ?", parameters=("t01-key",))))
        self.assertEqual(2, self.store.current_version("alpha"))

    @case("T02")
    def test_T02_the_same_key_with_a_different_request_raises_and_keeps_the_first_result(self):
        material = self.add_source(self.alpha, tag="a")
        evidence_id = material["evidence_id"]
        first_changeset = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("The first statement.", evidence=[evidence_id])],
            base_version=0,
        )
        first = self.commit_changeset(first_changeset, key="t02-key", base_version=0)
        self.assertEqual(1, first.created_claims)

        changed = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("A different statement.", evidence=[evidence_id])],
            base_version=1,
        )
        with self.assertRaises(IdempotencyError):
            self.commit_changeset(changed, key="t02-key", base_version=1)

        repurposed = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-other",
            claims=[claim("The first statement.", evidence=[evidence_id])],
            base_version=0,
        )
        with self.assertRaises(IdempotencyError):
            self.commit_changeset(repurposed, key="t02-key", base_version=0, run_id="run-other")

        self.assertEqual(1, self.store.current_version("alpha"))
        self.assertEqual(["The first statement."], self.statements())
        self.assertEqual(1, len(self.items("knowledge_submissions")))
        self.assertEqual(1, len(self.items("claim_versions")))

    @case("T03")
    def test_T03_a_reimport_reuses_the_revision_and_a_new_source_only_appends_provenance(self):
        first_material = self.add_source(self.alpha, tag="1", text="The harness owns deployment.")
        again = self.store.freeze_revision(
            scope=self.alpha,
            source_id=first_material["revision"]["source_id"],
            source_type="file",
            label="material-1",
            raw_content="The harness owns deployment.",
        )
        self.assertTrue(again["reused"])
        self.assertEqual(first_material["revision"]["revision_id"], again["revision_id"])
        self.assertEqual(1, len(self.items("source_revisions")))

        seeded = self.commit(
            "t03-seed",
            [claim("The harness owns deployment.", evidence=[first_material["evidence_id"]])],
            base_version=0,
        )
        claim_id = seeded.created_claim_ids[0]
        reimport = self.commit(
            "t03-reimport",
            [claim("The harness owns deployment.", evidence=[first_material["evidence_id"]])],
            base_version=1,
        )
        self.assertEqual("noop", reimport.status)
        self.assertEqual(1, reimport.knowledge_version)
        self.assertGreaterEqual(reimport.unchanged_claims, 1)
        self.assertEqual((), reimport.created_claim_ids)
        self.assertEqual((), reimport.updated_claim_ids)
        self.assertEqual(1, self.store.current_version("alpha"))
        self.assertEqual(1, len(self.items("claim_versions")))

        second_material = self.add_source(self.alpha, tag="2", text="The harness owns deployment, confirmed in review.")
        appended = self.commit(
            "t03-new-source",
            [
                claim(
                    "The harness owns deployment.",
                    evidence=[second_material["evidence_id"]],
                    origins=[{"derivation": "explicit", "evidence_refs": [second_material["evidence_id"]]}],
                )
            ],
            base_version=1,
        )
        self.assertEqual((), appended.created_claim_ids)
        self.assertEqual([claim_id], list(appended.updated_claim_ids))
        stored = self.store.get_claim(claim_id, self.alpha)
        self.assertEqual([1, 2], [row["version"] for row in stored["history"]])
        self.assertEqual(
            {first_material["evidence_id"], second_material["evidence_id"]},
            {evidence_id for origin in stored["origins"] for evidence_id in origin["evidence_refs"]},
        )
        self.assertEqual(1, len(self.store.iter_claims(self.alpha)))

    @case("T05")
    def test_T05_a_refused_commit_is_retried_safely_and_a_lost_response_replays_the_stored_result(self):
        material = self.add_source(self.alpha, tag="a")
        foreign = self.add_source(self.beta, tag="b")

        refused = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("The harness owns deployment.", evidence=[foreign["evidence_id"]])],
            base_version=0,
        )
        with self.assertRaises(ScopeError):
            self.commit_changeset(refused, key="t05-key", base_version=0)
        self.assertEqual(0, self.store.current_version("alpha"))
        self.assertEqual([], self.items("knowledge_submissions"))

        corrected = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("The harness owns deployment.", evidence=[material["evidence_id"]])],
            base_version=0,
        )
        first = self.commit_changeset(corrected, key="t05-key", base_version=0)
        self.assertEqual(1, first.created_claims)
        self.assertEqual(1, self.store.current_version("alpha"))

        replayed = self.commit_changeset(corrected, key="t05-key", base_version=0)
        self.assertEqual(first, replayed)
        self.assertEqual(1, first.knowledge_version)
        self.assertEqual(1, self.store.current_version("alpha"))
        self.assertEqual(1, len(self.store.iter_claims(self.alpha)))
        self.assertEqual(1, len(self.items("claim_versions")))
        self.assertEqual(1, len(self.items("knowledge_submissions")))

    @case("T06")
    def test_T06_a_stale_base_version_conflicts_and_the_stage_cache_follows_every_fingerprint(self):
        material = self.add_source(self.alpha, tag="a")
        self.commit("t06-first", [claim("The harness owns deployment.", evidence=[material["evidence_id"]])], base_version=0)
        self.assertEqual(1, self.store.current_version("alpha"))

        stale = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("A second statement.", evidence=[material["evidence_id"]])],
            base_version=0,
        )
        with self.assertRaises(ConflictError) as error:
            self.commit_changeset(stale, key="t06-stale", base_version=0)
        self.assertEqual(1, error.exception.current_version)
        self.assertEqual(1, self.store.current_version("alpha"))
        self.assertEqual(["The harness owns deployment."], self.statements())
        self.assertEqual([], self.items("knowledge_submissions", where="idempotency_key = ?", parameters=("t06-stale",)))

        fresh = build_change_set(
            knowledge_space_id=SPACE,
            project_id="alpha",
            run_id="run-1",
            claims=[claim("A second statement.", evidence=[material["evidence_id"]])],
            base_version=1,
        )
        retried = self.commit_changeset(fresh, key="t06-stale", base_version=1)
        self.assertEqual("committed", retried.status)
        self.assertEqual(2, retried.knowledge_version)

        output = {"candidates": ["one"]}
        first = self.store.stage_artifact(
            run_id="run-1", stage="extract", input_fingerprint="fp-1", output=output, prompt_version="prompt-1", model_id="model-1"
        )
        again = self.store.stage_artifact(
            run_id="run-1", stage="extract", input_fingerprint="fp-1", output=output, prompt_version="prompt-1", model_id="model-1"
        )
        other_prompt = self.store.stage_artifact(
            run_id="run-1", stage="extract", input_fingerprint="fp-1", output=output, prompt_version="prompt-2", model_id="model-1"
        )
        other_model = self.store.stage_artifact(
            run_id="run-1", stage="extract", input_fingerprint="fp-1", output=output, prompt_version="prompt-1", model_id="model-2"
        )
        other_input = self.store.stage_artifact(
            run_id="run-1", stage="extract", input_fingerprint="fp-2", output=output, prompt_version="prompt-1", model_id="model-1"
        )
        self.assertEqual(
            [False, True, False, False, False],
            [item["cached"] for item in (first, again, other_prompt, other_model, other_input)],
        )
        self.assertEqual(output, again["output"])
        self.assertEqual(4, len(self.items("stage_artifacts")))


class FaultInjectionTests(ClaimStoreTestCase):
    def make_store(self, database):
        return FaultingClaimStore(database, knowledge_space_id=SPACE)

    @case("T04")
    def test_T04_a_failure_before_the_commit_leaves_no_half_written_knowledge(self):
        """The three write points of one commit, faulted in turn.

        The relation and the review rows sit after the claim rows, and the version
        counter after all of them, so a failure at either leaves work behind to roll
        back if the transaction is not doing its job.
        """

        for marker in ("INTO claim_relations", "INTO review_queue", "UPDATE knowledge_meta SET knowledge_version"):
            with self.subTest(marker=marker):
                store = self.fresh_store()
                material = self.add_source(self.alpha, tag="a")
                seed = self.commit(
                    "t04-seed",
                    [
                        claim("First claim.", evidence=[material["evidence_id"]]),
                        claim("Second claim.", evidence=[material["evidence_id"]]),
                    ],
                    base_version=0,
                )
                first_version = self.version_of(seed.created_claim_ids[0])
                second_version = self.version_of(seed.created_claim_ids[1])
                later = self.add_source(self.alpha, tag="b")
                changeset = build_change_set(
                    knowledge_space_id=SPACE,
                    project_id="alpha",
                    run_id="run-fault",
                    claims=[claim("The faulted claim.", evidence=[later["evidence_id"]])],
                    relations=[edge("supports", first_version, second_version)],
                    reviews=[review("clm_" + "c" * 32, "unit-1", trigger_code="ambiguous_identity")],
                    base_version=1,
                )
                store.fail_on(marker)
                with self.assertRaises(ClaimStoreError):
                    self.commit_changeset(changeset, key="t04-faulted", base_version=1, run_id="run-fault")
                self.assertEqual(1, store.plan.fired)
                self.assertEqual(
                    {
                        "claims": 2,
                        "claim_versions": 2,
                        "claim_origins": 2,
                        "claim_evidence": 2,
                        "claim_relations": 0,
                        "review_queue": 0,
                        "knowledge_submissions": 1,
                        "stage_artifacts": 0,
                    },
                    self.table_counts(
                        "claims",
                        "claim_versions",
                        "claim_origins",
                        "claim_evidence",
                        "claim_relations",
                        "review_queue",
                        "knowledge_submissions",
                        "stage_artifacts",
                    ),
                )
                self.assertEqual(1, self.store.current_version("alpha"))
                self.assertEqual({"First claim.", "Second claim."}, set(self.statements()))

                corrected = build_change_set(
                    knowledge_space_id=SPACE,
                    project_id="alpha",
                    run_id="run-fault",
                    claims=[claim("The faulted claim.", evidence=[later["evidence_id"]])],
                    base_version=1,
                )
                retry = self.commit_changeset(corrected, key="t04-faulted", base_version=1, run_id="run-fault")
                self.assertEqual("committed", retry.status)
                self.assertEqual(2, retry.knowledge_version)
                self.assertEqual(1, retry.created_claims)

    @case("T04")
    def test_T04_a_run_that_never_committed_does_not_read_as_completed(self):
        self.ensure_run(self.alpha, "run-never-committed")
        with self.assertRaises(ClaimStoreError):
            self.store.advance_run("run-never-committed", "completed")
        self.assertEqual("received", self.store.run_status("run-never-committed")["status"])
        self.store.advance_run("run-never-committed", "failed")
        self.assertEqual("failed", self.store.run_status("run-never-committed")["status"])
        with self.assertRaises(ClaimStoreError):
            self.store.advance_run("run-never-committed", "completed")


if __name__ == "__main__":
    unittest.main()
