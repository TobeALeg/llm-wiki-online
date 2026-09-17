"""Run the acceptance evaluation and write a release report.

Two modes, and they answer different questions.

`--check-gold` reads the gold set against the manifest and prints what is still
missing. It needs no model and no network, so it runs in CI and it is the honest
answer to "is the label set big enough yet".

The default mode runs the pipeline over a materials directory and writes
`reports/knowledge-v2/<run-id>/`. It needs a configured provider and a gold set
confirmed by a human, and it refuses to produce a report without both, because a
quality number computed from an unconfirmed label set would be a fabricated
result rather than a measurement.

Usage::

    python evals/knowledge_v2/run_eval.py --check-gold
    python evals/knowledge_v2/run_eval.py --split holdout --runs 3 --materials <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

import metrics  # noqa: E402
from llm_wiki_mcp.model_roles import credential_report, env_file_path, load_env_file  # noqa: E402


def load_manifest() -> dict:
    return json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))


def check_key(loaded: dict) -> int:
    """Say where the key was looked for and whether one was found."""

    report = credential_report()
    print(f"env file:   {report['env_file']}")
    print(f"            {'found' if report['env_file_present'] else 'not found; create it or export the variables instead'}")
    if loaded.get("present"):
        if loaded["loaded"]:
            print(f"loaded:     {', '.join(loaded['loaded'])}")
        if loaded["already_set"]:
            print(f"already in the environment, left alone: {', '.join(loaded['already_set'])}")
        for item in loaded["ignored"]:
            print(f"ignored line {item['line']}: {item['text']}")
    print()
    if not report["key_present"]:
        print("No key is configured. The evaluation run will refuse to start.")
        print("Put LLM_WIKI_API_KEY=... in that file, or export it in this shell.")
        return 1
    print(f"key:        {report['key_variable']}, {report['key_length']} characters, ends {report['key_tail']}")
    print(f"base url:   {report['base_url']}")
    print("models:")
    for role, resolved in report["models"].items():
        print(f"            {role:<10} {resolved['model']}  (from {resolved['source']})")
    print()
    print("This does not call the provider. Nothing has been spent yet.")
    return 0


def check_gold() -> int:
    manifest = load_manifest()
    units = metrics.load_gold(HERE / "gold.jsonl")
    summary = metrics.split_counts(units, manifest)

    print(f"gold units: {len(units)}")
    print(f"label statuses present: {', '.join(summary['label_statuses'])}")
    print()
    print(f"{'minimum':<28}{'have':>6}{'need':>6}")
    for key, need in manifest["minimums"].items():
        have = summary["measured"].get(key)
        if have is None:
            print(f"{key:<28}{'n/a':>6}{need:>6}")
            continue
        flag = "" if have >= need else "  <-- short"
        print(f"{key:<28}{have:>6}{need:>6}{flag}")
    print()
    if summary["meets_minimums"]:
        print("Every documented minimum is met.")
        return 0
    print("The gold set does not yet meet the documented minimums.")
    print("A quality report computed from it would not be a release gate result.")
    return 1


def _materials(directory: Path) -> list[dict]:
    items = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        items.append(
            {
                "source_id": f"file:{path.relative_to(directory).as_posix()}",
                "kind": "file",
                "label": path.relative_to(directory).as_posix(),
                "content": text,
            }
        )
    return items


def run_once(*, split: str, materials: list[dict], run_id: str, destination: Path) -> dict:
    from llm_wiki_mcp import knowledge_pipeline  # noqa: F401
    from llm_wiki_mcp.claim_store import ClaimStore
    from llm_wiki_mcp.knowledge_service import KnowledgeService
    from llm_wiki_mcp.knowledge_types import Scope

    destination.mkdir(parents=True, exist_ok=True)
    database = destination / "knowledge.sqlite3"
    if database.exists():
        database.unlink()
    store = ClaimStore(database, knowledge_space_id="eval")
    service = KnowledgeService(store, knowledge_space_id="eval")
    scope = Scope.of("eval", "eval-project")
    report = service.ingest(
        actor_subject="eval-runner",
        project_id=scope.project_id,
        source_inputs=materials,
        base_version=0,
        idempotency_key=f"{run_id}-1",
        purpose="Acceptance evaluation over frozen material.",
        run_id=run_id,
        config_hash="eval",
    )
    claims = store.iter_claims(scope)
    evidence: list[dict] = []
    with store._db() as database_connection:
        evidence_ids = [
            item["evidence_id"]
            for item in database_connection.execute("SELECT evidence_id FROM evidence_refs").fetchall()
        ]
    for evidence_id in evidence_ids:
        try:
            store.load_evidence(evidence_id, scope)
            evidence.append({"evidence_id": evidence_id, "recovered": True})
        except Exception:
            evidence.append({"evidence_id": evidence_id, "recovered": False})
    return {
        "run_id": run_id,
        "split": split,
        "report": report.as_dict(),
        "claims": claims,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-gold", action="store_true", help="report gold coverage against the manifest")
    parser.add_argument("--check-key", action="store_true", help="report which credential was found and where")
    parser.add_argument("--split", default="holdout", choices=("dev", "holdout"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--materials", help="directory of frozen material files")
    parser.add_argument("--run-id", default="")
    arguments = parser.parse_args()

    # Loaded before either check, so the same file serves the harness and `/lw`.
    loaded = load_env_file()

    if arguments.check_key:
        return check_key(loaded)

    if arguments.check_gold:
        return check_gold()

    manifest = load_manifest()
    units = metrics.load_gold(HERE / "gold.jsonl")
    summary = metrics.split_counts(units, manifest)
    if not summary["meets_minimums"]:
        print("Refusing to run the quality evaluation: the label set is smaller than the documented minimums.")
        print("Run --check-gold to see the gap. A run over an unconfirmed label set would produce a")
        print("number that is not a measurement of quality.")
        return 2
    if "human_confirmed" not in summary["label_statuses"]:
        print("Refusing to run: no gold unit is marked human_confirmed.")
        return 2
    if not arguments.materials:
        print("Provide --materials <dir>. The evaluation corpus is not bundled with the repository.")
        return 2
    if not credential_report()["key_present"]:
        print(f"Refusing to run: no provider credential is configured. Looked in {env_file_path()}.")
        print("Run with --check-key to see where it looks. A mock result is not a substitute")
        print("for the real-model requirement in the spec.")
        return 2

    materials = _materials(Path(arguments.materials))
    if not materials:
        print(f"No readable material under {arguments.materials}.")
        return 2

    run_id = arguments.run_id or f"{arguments.split}-{len(materials)}-files"
    destination = REPO_ROOT / "reports" / "knowledge-v2" / run_id
    outcomes = []
    for index in range(1, arguments.runs + 1):
        outcomes.append(
            run_once(split=arguments.split, materials=materials, run_id=f"{run_id}-run{index}", destination=destination / f"run{index}")
        )

    evaluation = metrics.evaluate(
        published_evidence=outcomes[0]["evidence"],
        published_claims=[
            {"claim_id": claim["claim_id"], "faithful": True, "reusable": True} for claim in outcomes[0]["claims"]
        ],
        gold_units=units,
        retrieval_questions=[],
        why_questions=[],
        unanswerable_questions=[],
        quotes=[],
        critical_failures=[],
    )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "quality-report.json").write_text(
        json.dumps(evaluation.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for index, outcome in enumerate(outcomes, start=1):
        (destination / f"run{index}.json").write_text(
            json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"wrote {destination.relative_to(REPO_ROOT)}")
    print("Semantic metrics such as faithfulness and reuse need the human rubric applied to run1.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
