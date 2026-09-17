"""Read the review page's export and turn it into labels.

The page is where a person decides; this is where the decision becomes a label. Three
outcomes, and the difference between them is the point.

A unit marked 通过 becomes `human_confirmed`, with the corrected wording if they wrote
one. A unit marked 不通过 is written to a separate rejected file with its comment
rather than deleted, because a label set whose history is invisible cannot be
audited. A unit marked 拿不准 stays out of the confirmed set and is listed, because a
second pass is not a pass.

A unit flagged as a zero-tolerance error is collected separately. That list is what
blocks a release regardless of how good the aggregate metrics look, so it is an output
of the review rather than a metric computed over it.

Usage::

    python evals/knowledge_v2/apply_review.py --annotations review-annotations.json
    python evals/knowledge_v2/apply_review.py --annotations ... --write
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DRAFT_PATH = HERE / "gold.draft.jsonl"
GOLD_PATH = HERE / "gold.jsonl"
REJECTED_PATH = HERE / "gold.rejected.jsonl"
CRITICAL_PATH = HERE / "critical_failures.jsonl"

VERDICTS = ("pass", "fail", "unsure")


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def confirm(unit: dict, annotation: dict) -> dict:
    """A passed draft, as a label."""

    confirmed = dict(unit)
    confirmed["label_status"] = "human_confirmed"
    confirmed["reviewed_at"] = str(annotation.get("reviewed_at") or "")
    corrected = str(annotation.get("corrected_meaning") or "").strip()
    if corrected:
        confirmed["draft_meaning"] = str(unit.get("expected_meaning") or "")
        confirmed["expected_meaning"] = corrected
    confirmed.pop("machine_notes", None)
    confirmed["review_comment"] = str(annotation.get("comment") or "").strip()
    return confirmed


def reject(annotation: dict, unit: dict) -> dict:
    """A failed draft, kept so the decision is visible rather than erased."""

    return {
        "unit_id": annotation.get("unit_id"),
        "group_id": annotation.get("group_id"),
        "draft_from": annotation.get("draft_from"),
        "verdict": "fail",
        "comment": str(annotation.get("comment") or ""),
        "draft_meaning": str(unit.get("expected_meaning") or "")[:500],
        "reason": "reviewer rejected this draft",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--keep-constructed", action="store_true", default=True)
    arguments = parser.parse_args()

    path = Path(arguments.annotations).expanduser()
    if not path.is_file():
        print(f"No annotations at {path}.")
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    annotations = payload.get("annotations") or []
    drafts = {str(unit["unit_id"]): unit for unit in load_jsonl(DRAFT_PATH)}

    unknown = [item for item in annotations if str(item.get("unit_id")) not in drafts]
    if unknown:
        print(f"{len(unknown)} annotation(s) name a unit that is not in the draft file:")
        for item in unknown[:5]:
            print(f"  {item.get('unit_id')}")
        print("The draft file may have been regenerated since the page was built.")
        return 2
    for item in annotations:
        verdict = str(item.get("verdict") or "")
        if verdict and verdict not in VERDICTS:
            print(f"Unknown verdict {verdict!r} on {item.get('unit_id')}.")
            return 2

    passed = [item for item in annotations if item.get("verdict") == "pass"]
    failed = [item for item in annotations if item.get("verdict") == "fail"]
    unsure = [item for item in annotations if item.get("verdict") == "unsure"]
    untouched = [item for item in annotations if not item.get("verdict")]
    critical = [item for item in annotations if item.get("zero_tolerance")]

    print(f"annotations: {len(annotations)} of {len(drafts)} draft units")
    print(f"  pass {len(passed)} | fail {len(failed)} | unsure {len(unsure)} | untouched {len(untouched)}")
    print(f"  flagged as zero-tolerance: {len(critical)}")
    if critical:
        for item in critical[:10]:
            print(f"    {item['unit_id']}  {str(item.get('comment') or '')[:70]}")

    if not arguments.write:
        print("\nnothing written; pass --write to update the label files")
        return 0

    confirmed = [confirm(drafts[str(item["unit_id"])], item) for item in passed]
    rejected = [reject(item, drafts[str(item["unit_id"])]) for item in failed]
    critical_rows = [
        {
            "unit_id": item["unit_id"],
            "group_id": item.get("group_id"),
            "comment": str(item.get("comment") or ""),
            "meaning": str(drafts[str(item["unit_id"])].get("expected_meaning") or "")[:300],
        }
        for item in critical
    ]

    constructed = [
        unit for unit in load_jsonl(GOLD_PATH) if unit.get("label_status") == "constructed_unconfirmed"
    ]
    merged = constructed + confirmed
    write_jsonl(GOLD_PATH, merged)
    write_jsonl(REJECTED_PATH, rejected)
    write_jsonl(CRITICAL_PATH, critical_rows)

    print(f"\nwrote {GOLD_PATH.relative_to(REPO_ROOT)} ({len(merged)} units: "
          f"{len(constructed)} constructed + {len(confirmed)} confirmed)")
    if rejected:
        print(f"wrote {REJECTED_PATH.relative_to(REPO_ROOT)} ({len(rejected)} rejected drafts)")
    if critical_rows:
        print(f"wrote {CRITICAL_PATH.relative_to(REPO_ROOT)} ({len(critical_rows)} zero-tolerance findings)")
    if unsure:
        print(f"\n{len(unsure)} unit(s) marked 拿不准 need a second pass before they can be labels:")
        for item in unsure[:10]:
            print(f"  {item['unit_id']}  {str(item.get('comment') or '')[:70]}")
    if untouched:
        print(f"\n{len(untouched)} unit(s) were not reviewed and are not in the label set.")
    print("\nNext: python evals/knowledge_v2/run_eval.py --check-gold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
