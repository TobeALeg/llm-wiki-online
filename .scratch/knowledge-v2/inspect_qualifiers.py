"""Inspect what the real model produced for the qualifiers material, gate by gate."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import knowledge_pipeline, model_roles  # noqa: E402
from llm_wiki_mcp.knowledge_types import Scope  # noqa: E402

MATERIAL = (
    "# 存储选型记录\n\n"
    "我们仅在当前低数据量场景使用 SQLite。暂不引入 Postgres，"
    "除非单库超过 50GB 或者出现跨机房写入需求。这条结论仅限当前项目，"
    "不适用于公司其他项目。\n"
)


def main() -> int:
    model_roles.load_env_file()
    from llm_wiki_mcp.model import configured_model

    scope = Scope.of("local", "real-probe")
    prepared = knowledge_pipeline.prepare_ingest(
        scope=scope,
        source_inputs=[
            {"source_id": "probe:qualifiers", "kind": "conversation", "label": "q", "content": MATERIAL}
        ],
        config={"purpose": "Capture durable project knowledge.", "base_version": 0},
    )
    roles = knowledge_pipeline.ModelRoles.from_mapping(
        {"discovery": configured_model(), "reasoning": configured_model(), "grounding": configured_model()}
    )
    batch = knowledge_pipeline.extract_claims(
        prepared_run=prepared, history_reader=lambda s, c: [], model_roles=roles
    )
    print("=== candidates, before the final gate ===")
    for candidate in batch["candidates"]:
        state = candidate.state
        print(
            f"  disposition={candidate.disposition} kind={state.knowledge_kind} "
            f"derivation={state.derivation} epistemic={state.epistemic_status} "
            f"decision={state.decision_state}"
        )
        print(f"    statement: {candidate.statement}")
        print(f"    conditions: {list(candidate.conditions)}")
        print(f"    subjects: {list(candidate.subjects)}")
        print(f"    reason_codes: {list(candidate.reason_codes)}")
        print(f"    evidence_refs: {list(candidate.evidence_refs)}")

    validated = knowledge_pipeline.validate_changes(
        candidate_batch=batch,
        snapshot={"base_version": 0, "scope": prepared["scope"], "run_id": prepared["run_id"]},
    )
    print("\n=== published claims ===")
    for claim in validated["changeset"]["claims"]:
        print(f"  kind={claim['state']['knowledge_kind']} decision={claim['state'].get('decision_state')}")
        print(f"    statement: {claim['statement']}")
        print(f"    conditions: {claim.get('conditions')}")
        print(f"    support: {claim.get('support')}")
    print(f"\n=== rejected: {len(validated['rejected'])} ===")
    for item in validated["rejected"]:
        print(f"  {json.dumps(item, ensure_ascii=False)[:300]}")
    print(f"\n=== reviews: {len(validated['reviews'])} / dropped: {len(validated['dropped'])} ===")
    for item in validated["dropped"]:
        print(f"  dropped: {json.dumps(item, ensure_ascii=False)[:200]}")

    print("\n=== notes from the batch records ===")
    for record in batch["batches"]:
        for note in record.get("notes") or ():
            print(f"  {json.dumps(note, ensure_ascii=False)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
