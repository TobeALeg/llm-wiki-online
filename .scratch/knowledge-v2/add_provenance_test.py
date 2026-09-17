"""Confirm the pipeline surfaces per-stage model provenance."""

import sys
from pathlib import Path

REPO = Path(r"E:/llw-wiki-online")
sys.path.insert(0, str(REPO / "plugins" / "llm-wiki"))
sys.path.insert(0, str(REPO / "tests"))

from llm_wiki_mcp import knowledge_pipeline as pipeline
from llm_wiki_mcp import model_roles

notes = [
    {"stage": "discovery", "statement": "s", "note": "a problem"},
    {pipeline.PROVENANCE_KEY: model_roles.stage_record(
        role="discovery",
        resolved=model_roles.resolve_model("discovery", {"LLM_WIKI_DISCOVERY_MODEL": "cheap"}),
        prompt_version="2.0.0",
        attempts=1,
        usage=model_roles.usage_from_response({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
        base_url="https://api.example",
    )},
]
records = pipeline.stage_provenance(notes)
print("records:", records)
assert len(records) == 1, records
assert records[0]["model"] == "cheap"
assert records[0]["input_tokens"] == 7
assert records[0]["prompt_version"] == "2.0.0"
print("problem notes left alone:", [n for n in notes if "note" in n])
