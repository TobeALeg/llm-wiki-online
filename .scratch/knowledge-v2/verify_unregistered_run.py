"""Verify a commit and a review resolve without any registered run."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "plugins/llm-wiki")
from llm_wiki_mcp import chunking as ck, evidence as ev, knowledge_types as kt
from llm_wiki_mcp.claim_store import ClaimStore


def material(store, scope, text, source_id):
    revision = store.freeze_revision(
        scope=scope, source_id=source_id, source_type="file", label=source_id, raw_content=text
    )
    normalized = ev.normalize_text(text)
    artifact = ev.freeze_artifact(
        revision_id=revision["revision_id"],
        text=normalized,
        parser_name=ck.PARSER_NAME,
        parser_version=ck.PARSER_VERSION,
        config_hash="c",
        structure=ck.artifact_structure(normalized),
    )
    store.store_artifact(scope=scope, artifact=artifact, chunks=ck.chunk_text(normalized))
    start = normalized.index("SQLite")
    record = ev.make_evidence(project_id="demo", artifact=artifact, spans=[(start, start + 6)])
    store.register_evidence(record, scope=scope)
    return record


def main() -> int:
    directory = tempfile.mkdtemp()
    store = ClaimStore(Path(directory) / "knowledge.sqlite3")
    scope = kt.Scope.of("local", "demo")
    record = material(store, scope, "仅在低数据量场景使用 SQLite。\n", "src_a")

    changeset = kt.build_change_set(
        knowledge_space_id="local",
        project_id="demo",
        run_id="run-never-registered",
        base_version=0,
        claims=[
            {
                "statement": "仅在低数据量场景使用 SQLite。",
                "state": {
                    "knowledge_kind": "constraint",
                    "derivation": "explicit",
                    "epistemic_status": "asserted",
                },
                "origins": [{"derivation": "explicit", "evidence_refs": [record.evidence_id]}],
                "support": ["evidence"],
                "topic_ids": [],
            }
        ],
        dropped=[
            {
                "unit_id": "u1",
                "statement": "今天试了个工具",
                "reason_codes": ["ephemeral_activity"],
            }
        ],
        reviews=[
            {
                "review_id": "revq_1",
                "subject_kind": "claim",
                "subject_id": "clm_" + "a" * 32,
                "subject_version": "",
                "question": "同义吗",
                "trigger_code": "ambiguous_identity",
            }
        ],
    )
    outcome = store.commit_changes(
        actor_subject="a", base_version=0, idempotency_key="K1", changeset=changeset, project_id="demo"
    )
    print("commit:", outcome.status, "| drops:", outcome.dropped_candidates, "| reviews:", outcome.review_pending)
    print("dispositions:", [(item["statement"], item["reason_codes"]) for item in store.dispositions(scope)])

    reviews = store.open_reviews(scope)
    print("open reviews:", len(reviews))
    try:
        store.review_action(
            actor_subject="a",
            review_id=reviews[0]["review_id"],
            expected_version="",
            action="confirm_identity",
            scope=scope,
            idempotency_key="RA0",
            topic_id="top_" + "c" * 32,
        )
    except Exception as error:
        print("unknown topic refused:", type(error).__name__, str(error)[:70])
    topic_id = store.ensure_topic(scope=scope, canonical_label="Harness")
    action = store.review_action(
        actor_subject="a",
        review_id=reviews[0]["review_id"],
        expected_version="",
        action="confirm_identity",
        scope=scope,
        idempotency_key="RA1",
        topic_id=topic_id,
    )
    print("review action:", action["action"], "| remaining open:", len(store.open_reviews(scope)))

    try:
        store.stage_artifact(run_id="run-never-registered", stage="x", input_fingerprint="f", output={})
    except Exception as error:
        print("stage cache without a run:", type(error).__name__, str(error)[:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
