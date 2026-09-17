"""Compute the release metrics from one evaluation run.

The numbers are the ones the acceptance spec fixes in its section 8.2. They live
here as data, so a report can be recomputed and a threshold cannot be quietly
adjusted for one release. Every metric reports its numerator and denominator
beside its value, because a percentage without the case count behind it cannot be
audited.

A metric with a zero denominator is NOT EVALUATED, never a pass. That matters
most for the synthesis metrics: a pipeline that synthesizes nothing would
otherwise score a perfect accuracy, which is why the spec also gates synthesis
coverage and this module keeps the two separate.

Standard library only, so the report generator runs wherever the tests run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# The zero-tolerance list from the spec. Any one of these fails the release
# regardless of how good the aggregate metrics are.
CRITICAL_FAILURES = (
    "fabricated_adopted_decision",
    "cross_project_or_cross_space",
    "hypothesis_as_verified",
    "reconstructed_reason_as_quote",
    "unauthorized_decision_change",
    "fabricated_quote",
    "substituted_evidence_after_corruption",
)


@dataclass(frozen=True)
class Threshold:
    """One release gate. `minimum` for a floor, `maximum` for a ceiling."""

    metric_id: str
    title: str
    minimum: float | None = None
    maximum: float | None = None
    note: str = ""

    def verdict(self, value: float | None) -> str:
        if value is None:
            return "not_evaluated"
        if self.minimum is not None and value < self.minimum:
            return "fail"
        if self.maximum is not None and value > self.maximum:
            return "fail"
        return "pass"


THRESHOLDS = (
    Threshold("evidence_recovery", "证据可恢复率", minimum=1.0, note="针对应可用的测试证据"),
    Threshold("claim_faithfulness", "Claim 忠实度", minimum=0.95),
    Threshold("must_keep_coverage", "必须知识覆盖率", minimum=0.90),
    Threshold("decision_constraint_coverage", "关键决定/约束覆盖率", minimum=0.95),
    Threshold("reusable_rate", "可直接复用率", minimum=0.85),
    Threshold("synthesis_validity", "综合结论有效率", minimum=0.90),
    Threshold("synthesis_coverage", "合法综合目标覆盖率", minimum=0.80),
    Threshold("duplication_rate", "重复率", maximum=0.05),
    Threshold("recall_at_10", "Retrieval Recall@10", minimum=0.90),
    Threshold("why_pass_rate", "Why 回答通过率", minimum=0.90),
    Threshold("unknown_handling", "未记录答案的正确处理率", minimum=1.0),
    Threshold("quote_accuracy", "原话准确率", minimum=1.0),
)

THRESHOLDS_BY_ID = {threshold.metric_id: threshold for threshold in THRESHOLDS}


@dataclass(frozen=True)
class Ratio:
    """A metric with the case counts that produced it."""

    metric_id: str
    numerator: int
    denominator: int
    skipped: int = 0

    @property
    def value(self) -> float | None:
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    def as_dict(self) -> dict[str, Any]:
        value = self.value
        threshold = THRESHOLDS_BY_ID.get(self.metric_id)
        return {
            "metric_id": self.metric_id,
            "title": threshold.title if threshold else self.metric_id,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "skipped": self.skipped,
            "value": value,
            "display": "N/A" if value is None else f"{value:.4f}",
            "verdict": threshold.verdict(value) if threshold else "not_evaluated",
        }


@dataclass
class EvaluationReport:
    metrics: list[Ratio] = field(default_factory=list)
    critical: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        by_id = {ratio.metric_id: ratio.as_dict() for ratio in self.metrics}
        blocking = [
            item["metric_id"] for item in by_id.values() if item["verdict"] == "fail"
        ]
        return {
            "metrics": by_id,
            "critical_failures": self.critical,
            "notes": self.notes,
            "blocking_metrics": sorted(blocking),
            "release_ready": not blocking and not self.critical,
        }


def _count(items: Iterable[Mapping[str, Any]], key: str) -> tuple[int, int]:
    total = 0
    hits = 0
    for item in items:
        if item.get("excluded"):
            continue
        total += 1
        if item.get(key):
            hits += 1
    return hits, total


def _retrieval_recall(questions: Sequence[Mapping[str, Any]]) -> Ratio:
    """Recall@10 pooled over questions, counting gold units not questions."""

    numerator = 0
    denominator = 0
    skipped = 0
    for question in questions:
        if question.get("excluded"):
            skipped += 1
            continue
        gold = list(question.get("gold_unit_ids", ()))
        retrieved = list(question.get("retrieved_top_10", ()))
        denominator += len(gold)
        numerator += len({unit for unit in gold if unit in set(retrieved)})
    return Ratio("recall_at_10", numerator, denominator, skipped)


def _unknown_handling(questions: Sequence[Mapping[str, Any]]) -> Ratio:
    """Of the questions with no recorded answer, how many did NOT invent one."""

    numerator = 0
    denominator = 0
    skipped = 0
    for question in questions:
        if question.get("excluded"):
            skipped += 1
            continue
        denominator += 1
        if not question.get("fabricated_answer", False):
            numerator += 1
    return Ratio("unknown_handling", numerator, denominator, skipped)


def _synthesis_metrics(claims: Sequence[Mapping[str, Any]], gold: Sequence[Mapping[str, Any]]) -> tuple[Ratio, Ratio]:
    synthesized = [claim for claim in claims if claim.get("synthesized") and not claim.get("excluded")]
    valid = 0
    for claim in synthesized:
        if claim.get("premises_complete") and claim.get("inference_acceptable") and claim.get("modality_correct"):
            valid += 1
    targets = [unit for unit in gold if unit.get("synthesis_target") and not unit.get("excluded")]
    formed = sum(1 for unit in targets if unit.get("synthesis_formed") and unit.get("retrievable"))
    return (
        Ratio("synthesis_validity", valid, len(synthesized)),
        Ratio("synthesis_coverage", formed, len(targets)),
    )


def evaluate(
    *,
    published_evidence: Sequence[Mapping[str, Any]] = (),
    published_claims: Sequence[Mapping[str, Any]] = (),
    gold_units: Sequence[Mapping[str, Any]] = (),
    retrieval_questions: Sequence[Mapping[str, Any]] = (),
    why_questions: Sequence[Mapping[str, Any]] = (),
    unanswerable_questions: Sequence[Mapping[str, Any]] = (),
    quotes: Sequence[Mapping[str, Any]] = (),
    critical_failures: Sequence[Mapping[str, Any]] = (),
) -> EvaluationReport:
    """Turn one run's raw outcomes into the twelve release metrics."""

    refuse_excluded_gold(gold_units)
    report = EvaluationReport()

    recovered, total_evidence = _count(published_evidence, "recovered")
    report.metrics.append(Ratio("evidence_recovery", recovered, total_evidence))

    faithful, total_claims = _count(published_claims, "faithful")
    report.metrics.append(Ratio("claim_faithfulness", faithful, total_claims))

    reusable, _ = _count(published_claims, "reusable")
    report.metrics.append(Ratio("reusable_rate", reusable, total_claims))

    redundant, published_total = _count(published_claims, "redundant")
    report.metrics.append(Ratio("duplication_rate", redundant, published_total))

    keep = [unit for unit in gold_units if unit.get("must_keep") and not unit.get("excluded")]
    kept = sum(1 for unit in keep if unit.get("retrievable"))
    report.metrics.append(Ratio("must_keep_coverage", kept, len(keep)))

    decision_units = [
        unit
        for unit in keep
        if unit.get("kind") in {"decision", "constraint"}
    ]
    decision_kept = sum(1 for unit in decision_units if unit.get("retained_correctly"))
    report.metrics.append(
        Ratio("decision_constraint_coverage", decision_kept, len(decision_units))
    )

    validity, coverage = _synthesis_metrics(published_claims, gold_units)
    report.metrics.append(validity)
    report.metrics.append(coverage)

    report.metrics.append(_retrieval_recall(retrieval_questions))

    why_correct, why_total = _count(why_questions, "correct")
    report.metrics.append(Ratio("why_pass_rate", why_correct, why_total))

    report.metrics.append(_unknown_handling(unanswerable_questions))

    exact, quote_total = _count(quotes, "exact")
    report.metrics.append(Ratio("quote_accuracy", exact, quote_total))

    for failure in critical_failures:
        code = str(failure.get("code", ""))
        if code not in CRITICAL_FAILURES:
            raise ValueError(f"Unknown critical failure code: {code!r}")
        report.critical.append(dict(failure))

    if total_evidence == 0:
        report.notes.append("No published evidence: the recovery rate has nothing to measure.")
    if not any(claim.get("synthesized") for claim in published_claims):
        report.notes.append(
            "No synthesized claim was published, so synthesis validity is N/A. "
            "The synthesis coverage metric is what prevents that from reading as a pass."
        )
    return report


REVIEW_RATIO_TARGET = 0.15
"""The share of candidates that may end up waiting on a person.

An observation, not a gate. The spec is explicit that this must not become a
quota the pipeline satisfies by hiding ambiguity, adopting decisions on its own,
or dropping material worth keeping, so it is reported beside the blocking metrics
rather than among them.
"""


def review_burden(
    *,
    candidates: int,
    review_pending: int,
    material_groups: int = 0,
    review_cards: int = 0,
) -> dict[str, Any]:
    """How much human attention a batch asked for, measured the way the spec asks."""

    if candidates < 0 or review_pending < 0:
        raise ValueError("Counts cannot be negative.")
    if review_pending > candidates:
        raise ValueError("More reviews than candidates means the denominator is wrong.")
    ratio = None if candidates == 0 else review_pending / candidates
    return {
        "candidates": candidates,
        "review_pending": review_pending,
        "ratio": ratio,
        "display": "N/A" if ratio is None else f"{ratio:.4f}",
        "target": REVIEW_RATIO_TARGET,
        "within_target": None if ratio is None else ratio <= REVIEW_RATIO_TARGET,
        "material_groups": material_groups,
        "per_group": None if not material_groups else review_pending / material_groups,
        "review_cards": review_cards or review_pending,
        "note": (
            "Observation only. A ratio above the target is a reason to look at why, "
            "never a reason to suppress ambiguity, auto-adopt, or drop material."
        ),
    }


def load_gold(path: str | Path) -> list[dict[str, Any]]:
    """Read a gold set from JSONL. Each line carries its own label status."""

    units: list[dict[str, Any]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            units.append(json.loads(stripped))
        except json.JSONDecodeError as error:
            raise ValueError(f"Gold line {number} is not valid JSON: {error}") from error
    return units


def refuse_excluded_gold(units: Sequence[Mapping[str, Any]]) -> None:
    """A gold unit may not be excluded from the denominator.

    `excluded` exists so an evaluation run can skip a row it genuinely cannot
    score, and a run reports how many it skipped. Marking a row excluded in the
    gold file itself would drop a failing sample out of the denominator, which is
    the exact way the spec forbids raising a score.
    """

    offenders = [str(unit.get("unit_id")) for unit in units if unit.get("excluded")]
    if offenders:
        raise ValueError(
            "These gold units are marked excluded, which removes them from every "
            "denominator. Fix the sample or report it as a failure: " + ", ".join(offenders)
        )


def split_counts(units: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The coverage of the gold set against the sizes the spec asks for."""

    refuse_excluded_gold(units)
    minimums = manifest["minimums"]
    by_split = {"dev": 0, "holdout": 0}
    for unit in units:
        by_split[str(unit.get("split", "dev"))] = by_split.get(str(unit.get("split", "dev")), 0) + 1
    must_keep = [unit for unit in units if unit.get("must_keep")]
    decision_units = [unit for unit in must_keep if unit.get("kind") in {"decision", "constraint"}]
    negative = [unit for unit in units if unit.get("negative_example")]
    synthesis = [unit for unit in units if unit.get("synthesis_target")]
    groups = {str(unit.get("group_id", "")) for unit in units}
    questions = [(unit, question) for unit in units for question in unit.get("future_questions", ())]

    measured = {
        "material_groups": len(groups),
        "must_keep": len(must_keep),
        "future_questions": len(questions),
        "decision_constraint": len(decision_units),
        "negative": len(negative),
        "synthesis_target": len(synthesis),
        "holdout_must_keep": sum(1 for unit in must_keep if unit.get("split") == "holdout"),
        "holdout_decision_constraint": sum(
            1 for unit in decision_units if unit.get("split") == "holdout"
        ),
        "holdout_negative": sum(1 for unit in negative if unit.get("split") == "holdout"),
        "holdout_synthesis_target": sum(1 for unit in synthesis if unit.get("split") == "holdout"),
        "holdout_future_questions": sum(
            1 for unit, _question in questions if unit.get("split") == "holdout"
        ),
    }
    shortfalls = {
        key: {"have": measured[key], "need": value}
        for key, value in minimums.items()
        if key in measured and measured[key] < value
    }
    return {
        "measured": measured,
        "shortfalls": shortfalls,
        "meets_minimums": not shortfalls,
        "label_statuses": sorted({str(unit.get("label_status", "unknown")) for unit in units}),
    }
