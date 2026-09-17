"""Capture what the real model actually returns, so the contract can accept it."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import knowledge_pipeline, model_roles, wiki_prompts  # noqa: E402
from llm_wiki_mcp.knowledge_types import Scope  # noqa: E402

MATERIAL = (
    "# 架构评审节选\n\n"
    "未来如果模型供应商增加到三家以上，企业可能需要独立的 Harness Layer 来统一"
    "评测与回退。现在还没有这个层，也没有决定要建。\n"
)


def shape(value, depth=0):
    """A description of a JSON value's shape, without printing content."""

    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {shape(item, depth + 1)}" for key, item in list(value.items())[:8]) + "}"
    if isinstance(value, list):
        return f"[{len(value)} x {shape(value[0], depth + 1) if value else 'empty'}]"
    return type(value).__name__


def main() -> int:
    model_roles.load_env_file()
    from llm_wiki_mcp.model import configured_model

    scope = Scope.of("local", "real-probe")
    prepared = knowledge_pipeline.prepare_ingest(
        scope=scope,
        source_inputs=[
            {"source_id": "probe:modality", "kind": "conversation", "label": "m", "content": MATERIAL}
        ],
        config={"purpose": "Capture durable project knowledge.", "base_version": 0},
    )
    batch = prepared["batches"][0]
    materials = [dict(item) for item in batch["materials"]]
    request = wiki_prompts.build_role_request(
        wiki_prompts.ROLE_DISCOVERY, materials=materials, purpose=prepared["purpose"]
    )
    print("request top-level keys:", sorted(request.keys()))
    answer = configured_model()(request, prepared["purpose"], materials)
    print("\nraw answer top-level shape:", shape(answer))
    print("raw answer top-level keys:", sorted(answer.keys()) if isinstance(answer, dict) else "(not a dict)")
    for key, value in (answer.items() if isinstance(answer, dict) else ()):
        print(f"  {key}: {shape(value)}")

    print("\n--- what the pipeline does with it ---")
    try:
        parsed = knowledge_pipeline._answer_candidates(answer, wiki_prompts.ROLE_DISCOVERY)
        print("parsed", len(parsed), "raw candidate(s)")
    except Exception as error:
        print(f"{type(error).__name__}: {error}")

    print("\n--- and with the shapes a model commonly returns instead ---")
    probe = {
        "a bare list": [{"statement": "x"}],
        "claims key": {"claims": [{"statement": "x"}]},
        "candidates and a note": {"candidates": [{"statement": "x"}], "note": "n"},
        "nested under result": {"result": {"candidates": [{"statement": "x"}]}},
        "items key": {"items": [{"statement": "x"}]},
    }
    for name, value in probe.items():
        try:
            parsed = knowledge_pipeline._answer_candidates(value, wiki_prompts.ROLE_DISCOVERY)
            print(f"  {name}: accepted ({len(parsed)})")
        except Exception as error:
            print(f"  {name}: refused with {type(error).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
