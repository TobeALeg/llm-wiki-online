"""Draft the gold set from real material, and show a person what to check.

What this produces, and what it refuses to produce.

It produces the mechanical half: run the real pipeline over each material group,
then turn what came back into draft gold units. Each draft carries the material's
own qualifiers, the over-claims that are visible from the text, the evidence
position, and questions the unit should be able to answer later.

It refuses to produce the judgement half. Every unit is written with
`label_status: draft_unconfirmed`, and `machine_notes` names which fields were
derived from the text and which need a person. A unit whose `expected_meaning` was
copied from the model's own statement is not a label, it is the model marking its
own work, and a score computed against it would measure agreement with itself.

The report is the review surface: material excerpt on the left, drafted unit on the
right, one block per unit, so correcting the labels is reading rather than writing.

Usage::

    python evals/knowledge_v2/draft_gold.py --material-root "/e/AI infra" --group bp-v14
    python evals/knowledge_v2/draft_gold.py --material-root "/e/AI infra" --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import knowledge_pipeline, model_roles  # noqa: E402
from llm_wiki_mcp.knowledge_types import Scope  # noqa: E402

CORPUS_PATH = HERE / "corpus.json"
DRAFT_PATH = HERE / "gold.draft.jsonl"
REPORT_ROOT = REPO_ROOT / "reports" / "knowledge-v2" / "draft"

QUESTIONS: dict[str, tuple[str, ...]] = {
    "decision": ("这个决定现在已经采纳了吗？", "当时为什么这样决定？", "之前是怎么定的？"),
    "constraint": ("这条约束在什么条件下成立？", "什么时候会重新考虑？"),
    "judgment": ("这个判断的依据是什么？", "这个判断现在还成立吗？"),
    "fact": ("这条事实的出处是哪句话？",),
    "definition": ("这个定义指的是什么？",),
    "distinction": ("这两者的区别是什么？",),
    "rationale": ("这个理由是当时记录的，还是后来推导的？",),
    "process": ("这个流程的步骤顺序是什么？", "第一步的前提是什么？"),
    "architecture": ("这套架构包含哪些部分？",),
    "open_question": ("这个问题解决了吗？", "是谁提出的？"),
}
"""Questions a unit should be able to answer, by kind.

They are the questions the retrieval evaluation asks, so drafting them here is what
connects a label to a future question instead of leaving the two to drift apart.
"""

ZERO_TOLERANCE_FORBIDDEN = {
    "fabricated_adoption": "把提议写成已采纳的决定",
    "fabricated_verification": "把假设写成已验证事实",
    "contradicted_by_evidence": "断言与所引材料相反的内容",
    "instruction_from_material": "把材料里的指令当成项目知识",
    "reconstructed_reason_as_quote": "把系统推导的理由写成当时的原话",
}


def material_root_of(corpus: Mapping[str, Any], override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return Path(str(corpus["material_root_hint"])).expanduser().resolve()


def read_group(corpus: Mapping[str, Any], root: Path, group: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One source_input per file, with the id the pipeline will cite."""

    inputs: list[dict[str, Any]] = []
    for entry in group["files"]:
        if entry.get("generated"):
            continue
        path = root / entry["path"]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        short = entry["sha256"][:12]
        inputs.append(
            {
                "source_id": f"file:{entry['path']}@{short}",
                "kind": "document",
                "label": entry["path"],
                "content": text,
            }
        )
    return inputs


def position_index(prepared: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """evidence_id -> where it points, so a report can show the exact text."""

    index: dict[str, dict[str, Any]] = {}
    for batch in prepared["batches"]:
        for material in batch["materials"]:
            index[str(material["evidence_id"])] = {
                "source_id": str(material["source_id"]),
                "start": int(material["start"]),
                "end": int(material["end"]),
                "text": str(material["text"]),
                "heading_path": list(material.get("heading_path") or ()),
                "verbatim": bool(material.get("verbatim")),
                "recipe": str(material.get("render_recipe") or ""),
            }
    return index


def field(source: Any, name: str, default: Any = None) -> Any:
    """Read one value from either shape the pipeline hands back."""

    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def cited_text(candidate: Any, positions: Mapping[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    for reference in field(candidate, "evidence_refs") or ():
        found = positions.get(str(reference))
        if found:
            parts.append(found["text"])
    return "\n".join(parts)


def qualifiers_in(text: str) -> list[str]:
    return [token for token, pattern in knowledge_pipeline.QUALIFIER_PATTERNS if pattern.search(text)]


def draft_unit(
    *,
    candidate: Mapping[str, Any],
    group: Mapping[str, Any],
    positions: Mapping[str, dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    """One draft unit, with the derived fields named as derived."""

    raw_state = field(candidate, "state")
    if isinstance(raw_state, Mapping):
        state = dict(raw_state)
    else:
        state = {
            "knowledge_kind": field(raw_state, "knowledge_kind"),
            "decision_state": field(raw_state, "decision_state"),
            "epistemic_status": field(raw_state, "epistemic_status"),
        }
    kind = str(state.get("knowledge_kind") or "fact")
    statement = str(field(candidate, "statement") or "")
    text = cited_text(candidate, positions)
    derived_qualifiers = qualifiers_in(text)
    adoption = knowledge_pipeline.adoption_markers(text)

    forbidden: list[str] = []
    notes: list[str] = []
    if state.get("decision_state") == "adopted" and not adoption:
        forbidden.append("该决定已采纳（材料没有采用标记）")
        notes.append("forbidden_assertions 由材料缺少采用标记推导")
    if state.get("decision_state") == "adopted" and adoption:
        notes.append(f"材料带采用标记 {adoption}，因此 adopted 有据")
    if state.get("epistemic_status") == "hypothesis":
        forbidden.append("该判断已验证或已成事实")
        notes.append("forbidden_assertions 由 hypothesis 推导")
    for qualifier in derived_qualifiers:
        carrying = qualifier in statement or qualifier in " ".join(field(candidate, "conditions") or ())
        if not carrying:
            forbidden.append(f"无条件成立（材料限定：{qualifier}）")
            notes.append(f"材料含限定 {qualifier}，候选未带，已记入 forbidden")

    targets = []
    for reference in field(candidate, "evidence_refs") or ():
        found = positions.get(str(reference))
        if found:
            targets.append(
                {
                    "source_fixture_id": found["source_id"],
                    "char_range": [found["start"], found["end"]],
                    "heading_path": found["heading_path"],
                }
            )

    return {
        "unit_id": f"{group['group_id']}-draft-{index:03d}",
        "group_id": group["group_id"],
        "split": group["split"],
        "project_id": "bp",
        "must_keep": True,
        "expected_meaning": statement,
        "required_qualifiers": derived_qualifiers,
        "forbidden_assertions": forbidden,
        "allowed_kinds": [kind],
        "evidence_targets": targets,
        "future_questions": list(QUESTIONS.get(kind, ())),
        "label_status": "draft_unconfirmed",
        "draft_from": "published_claim",
        "machine_notes": notes + [
            "expected_meaning 抄自模型自己的 statement，需要你判断它是不是该保留的知识",
            "must_keep 默认为 true，需要你确认",
        ],
    }


def draft_negative(
    *,
    item: Mapping[str, Any],
    group: Mapping[str, Any],
    index: int,
    origin: str,
) -> dict[str, Any]:
    code = str(item.get("code") or "")
    reason_codes = [str(value) for value in item.get("reason_codes") or ()]
    forbidden = [ZERO_TOLERANCE_FORBIDDEN[code]] if code in ZERO_TOLERANCE_FORBIDDEN else []
    return {
        "unit_id": f"{group['group_id']}-negative-{index:03d}",
        "group_id": group["group_id"],
        "split": group["split"],
        "project_id": "bp",
        "must_keep": False,
        "negative_example": True,
        "expected_meaning": str(item.get("statement") or item.get("detail") or "")[:500],
        "required_qualifiers": [],
        "forbidden_assertions": forbidden,
        "allowed_kinds": [],
        "evidence_targets": [],
        "future_questions": [],
        "label_status": "draft_unconfirmed",
        "draft_from": origin,
        "machine_notes": [
            f"来源：{origin}，代码 {code or 'DROP'}，理由 {reason_codes}",
            "这是一条负例草稿：需要你确认它确实不该长期保留，或者它其实值得保留",
        ],
    }


def draft_review(*, item: Mapping[str, Any], group: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "unit_id": f"{group['group_id']}-review-{index:03d}",
        "group_id": group["group_id"],
        "split": group["split"],
        "project_id": "bp",
        "must_keep": None,
        "requires_review": True,
        "expected_meaning": str(item.get("question") or ""),
        "required_qualifiers": [],
        "forbidden_assertions": [],
        "allowed_kinds": [],
        "evidence_targets": [],
        "future_questions": [],
        "label_status": "draft_unconfirmed",
        "draft_from": "review",
        "machine_notes": [
            f"系统不确定，触发原因 {item.get('trigger_code')}",
            "需要你决定：按原文补上限定后保留，还是按当前措辞发布，还是不要这条",
        ],
    }


def run_group(corpus: Mapping[str, Any], root: Path, group: Mapping[str, Any]) -> dict[str, Any]:
    inputs = read_group(corpus, root, group)
    if not inputs:
        return {"group_id": group["group_id"], "skipped": "no readable material", "units": []}
    scope = Scope.of("local", "bp")
    started = time.monotonic()
    prepared = knowledge_pipeline.prepare_ingest(
        scope=scope, source_inputs=inputs, config={"purpose": "评审商业计划中的决策、约束与限定。", "base_version": 0}
    )
    from llm_wiki_mcp.model import configured_model

    model = configured_model()
    roles = knowledge_pipeline.ModelRoles.from_mapping(
        {"discovery": model, "reasoning": model, "grounding": model}
    )
    batch = knowledge_pipeline.extract_claims(
        prepared_run=prepared, history_reader=lambda _scope, _candidate: [], model_roles=roles
    )
    validated = knowledge_pipeline.validate_changes(candidate_batch=batch, snapshot=prepared)
    positions = position_index(prepared)

    units: list[dict[str, Any]] = []
    index = 0
    by_statement = {str(field(candidate, "statement")): candidate for candidate in batch["candidates"]}
    for claim in validated["changeset"]["claims"]:
        index += 1
        units.append(
            draft_unit(
                candidate=by_statement.get(str(claim.get("statement")), claim),
                group=group,
                positions=positions,
                index=index,
            )
        )
    for item in validated["changeset"]["dropped"]:
        index += 1
        units.append(draft_negative(item=item, group=group, index=index, origin="value_gate_drop"))
    for item in validated["rejected"]:
        index += 1
        units.append(draft_negative(item=item, group=group, index=index, origin="final_gate_rejection"))
    for item in validated["reviews"]:
        index += 1
        units.append(draft_review(item=item, group=group, index=index))

    return {
        "group_id": group["group_id"],
        "split": group["split"],
        "title": group["title"],
        "seconds": round(time.monotonic() - started, 1),
        "run_status": knowledge_pipeline.run_status(batch),
        "batches": len(prepared["batches"]),
        "candidates": len(batch["candidates"]),
        "published": len(validated["changeset"]["claims"]),
        "dropped": len(validated["changeset"]["dropped"]),
        "rejected": len(validated["rejected"]),
        "reviews": len(validated["reviews"]),
        "unfinished": list(batch.get("unfinished") or ()),
        "units": units,
        "positions": positions,
        "provenance": knowledge_pipeline.stage_provenance(
            [note for record in batch["batches"] for note in record.get("notes") or ()]
        ),
    }


def render_report(outcome: Mapping[str, Any]) -> str:
    """The review surface: material on the left, drafted unit on the right."""

    lines = [
        f"# {outcome['title']} 标注复核（{outcome['group_id']}）",
        "",
        f"分区 `{outcome['split']}`，批次 {outcome['batches']}，候选 {outcome['candidates']}，"
        f"发布 {outcome['published']}，丢弃 {outcome['dropped']}，拒绝 {outcome['rejected']}，"
        f"待审 {outcome['reviews']}，耗时 {outcome['seconds']}s，run 状态 `{outcome['run_status']}`。",
        "",
        "每条草稿下面是左边的原文片段与右边的拟保留单元。需要你改的是 "
        "`must_keep`、`expected_meaning`、`forbidden_assertions`，以及负例是否真的是负例。",
        "",
    ]
    for unit in outcome["units"]:
        lines.append(f"## {unit['unit_id']}（{unit['draft_from']}）")
        lines.append("")
        excerpt = str(unit.get("expected_meaning") or "").strip().splitlines()
        lines.append("**左：拟保留的内容**")
        lines.append("")
        for line in excerpt[:6]:
            lines.append(f"> {line}")
        if len(excerpt) > 6:
            lines.append("> …")
        lines.append("")
        lines.append("**右：草稿单元**")
        lines.append("")
        lines.append(f"- must_keep: `{unit['must_keep']}`")
        lines.append(f"- allowed_kinds: `{unit['allowed_kinds']}`")
        if unit["required_qualifiers"]:
            lines.append(f"- required_qualifiers: `{unit['required_qualifiers']}`")
        if unit["forbidden_assertions"]:
            lines.append(f"- forbidden_assertions: `{unit['forbidden_assertions']}`")
        if unit["future_questions"]:
            lines.append(f"- future_questions: `{unit['future_questions']}`")
        if unit["evidence_targets"]:
            target = unit["evidence_targets"][0]
            lines.append(
                f"- 证据位置: `{target['source_fixture_id']}` 字符 "
                f"{target['char_range'][0]}–{target['char_range'][1]}"
                + (f"，标题路径 {'/'.join(target['heading_path'])}" if target["heading_path"] else "")
            )
        lines.append("")
        for note in unit.get("machine_notes") or ():
            lines.append(f"- 机器说明：{note}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--material-root")
    parser.add_argument("--group", action="append", help="only this group; repeatable")
    parser.add_argument("--write", action="store_true", help="write gold.draft.jsonl and the reports")
    parser.add_argument("--real-only", action="store_true", default=True)
    arguments = parser.parse_args()

    loaded = model_roles.load_env_file()
    report = model_roles.credential_report()
    if not report.get("key_present"):
        print("No provider key is configured. See run_eval.py --check-key.")
        return 2
    print(f"env file: {loaded['path']} | key ends {report.get('key_tail')!r} | {report.get('base_url')}")

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    root = material_root_of(corpus, arguments.material_root)
    wanted = set(arguments.group or ())
    groups = [
        group
        for group in corpus["groups"]
        if not group["constructed"] and (not wanted or group["group_id"] in wanted)
    ]
    if not groups:
        print("No material group matched.")
        return 2

    drafts: list[dict[str, Any]] = []
    for group in groups:
        print(f"\n=== {group['group_id']} ({group['split']}) {group['title']} ===")
        outcome = run_group(corpus, root, group)
        if outcome.get("skipped"):
            print(f"  skipped: {outcome['skipped']}")
            continue
        print(
            f"  {outcome['seconds']}s batches={outcome['batches']} candidates={outcome['candidates']} "
            f"published={outcome['published']} dropped={outcome['dropped']} "
            f"rejected={outcome['rejected']} reviews={outcome['reviews']}"
        )
        for record in outcome["provenance"]:
            print(f"  stage {record['role']}: {record['model']} tokens {record['input_tokens']}/"
                  f"{record['output_tokens']}")
        drafts.extend(outcome["units"])
        if arguments.write:
            REPORT_ROOT.mkdir(parents=True, exist_ok=True)
            (REPORT_ROOT / f"{group['group_id']}.md").write_text(render_report(outcome), encoding="utf-8")

    if arguments.write:
        with DRAFT_PATH.open("w", encoding="utf-8") as handle:
            for unit in drafts:
                handle.write(json.dumps(unit, ensure_ascii=False) + "\n")
        print(f"\nwrote {DRAFT_PATH.relative_to(REPO_ROOT)} ({len(drafts)} draft units)")
        print(f"wrote {REPORT_ROOT.relative_to(REPO_ROOT)}/*.md")

    print(f"\ndrafts: {len(drafts)}")
    counts: dict[str, int] = {}
    for unit in drafts:
        counts[unit["draft_from"]] = counts.get(unit["draft_from"], 0) + 1
    for origin, count in sorted(counts.items()):
        print(f"  {origin}: {count}")
    print("\nEvery unit is draft_unconfirmed. A score against these would measure the model's")
    print("agreement with itself, so run_eval still refuses until they are human_confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
