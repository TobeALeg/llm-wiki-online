"""The metric arithmetic is a gate, so the gate has its own gate.

If `evaluate` counted the wrong denominator, a release report would carry a
number nobody could reproduce. Each test here pins one metric against a literal
expected value and names the spec threshold it is compared against.
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "evals" / "knowledge_v2"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

import metrics  # noqa: E402


class RatioTests(unittest.TestCase):
    def test_a_zero_denominator_is_not_evaluated_and_never_a_pass(self):
        ratio = metrics.Ratio("synthesis_validity", 0, 0)
        self.assertIsNone(ratio.value)
        self.assertEqual(ratio.as_dict()["display"], "N/A")
        self.assertEqual(ratio.as_dict()["verdict"], "not_evaluated")

    def test_the_display_carries_the_counts_a_reviewer_needs(self):
        ratio = metrics.Ratio("claim_faithfulness", 19, 20)
        payload = ratio.as_dict()
        self.assertEqual(payload["numerator"], 19)
        self.assertEqual(payload["denominator"], 20)
        self.assertEqual(payload["value"], 0.95)
        self.assertEqual(payload["display"], "0.9500")
        self.assertEqual(payload["verdict"], "pass")

    def test_a_floor_fails_just_below_and_passes_exactly_at_the_threshold(self):
        self.assertEqual(metrics.THRESHOLDS_BY_ID["claim_faithfulness"].verdict(0.9499), "fail")
        self.assertEqual(metrics.THRESHOLDS_BY_ID["claim_faithfulness"].verdict(0.95), "pass")

    def test_a_ceiling_fails_just_above_and_passes_exactly_at_the_threshold(self):
        self.assertEqual(metrics.THRESHOLDS_BY_ID["duplication_rate"].verdict(0.0501), "fail")
        self.assertEqual(metrics.THRESHOLDS_BY_ID["duplication_rate"].verdict(0.05), "pass")

    def test_every_spec_threshold_is_present_once(self):
        expected = {
            "evidence_recovery": 1.0,
            "claim_faithfulness": 0.95,
            "must_keep_coverage": 0.90,
            "decision_constraint_coverage": 0.95,
            "reusable_rate": 0.85,
            "synthesis_validity": 0.90,
            "synthesis_coverage": 0.80,
            "recall_at_10": 0.90,
            "why_pass_rate": 0.90,
            "unknown_handling": 1.0,
            "quote_accuracy": 1.0,
        }
        for metric_id, minimum in expected.items():
            with self.subTest(metric=metric_id):
                self.assertAlmostEqual(metrics.THRESHOLDS_BY_ID[metric_id].minimum, minimum)
        self.assertAlmostEqual(metrics.THRESHOLDS_BY_ID["duplication_rate"].maximum, 0.05)
        self.assertEqual(len(metrics.THRESHOLDS), len(metrics.THRESHOLDS_BY_ID))


class EvaluateTests(unittest.TestCase):
    def _report(self, **overrides):
        arguments = {
            "published_evidence": [
                {"evidence_id": "evd_1", "recovered": True},
                {"evidence_id": "evd_2", "recovered": True},
                {"evidence_id": "evd_3", "recovered": False},
                {"evidence_id": "evd_4", "recovered": True},
            ],
            "published_claims": [
                {"claim_id": "clm_1", "faithful": True, "reusable": True, "redundant": False},
                {"claim_id": "clm_2", "faithful": True, "reusable": True, "redundant": False},
                {"claim_id": "clm_3", "faithful": True, "reusable": False, "redundant": False},
                {"claim_id": "clm_4", "faithful": False, "reusable": True, "redundant": True},
            ],
            "gold_units": [
                {"unit_id": "g1", "must_keep": True, "kind": "constraint", "retrievable": True, "retained_correctly": True},
                {"unit_id": "g2", "must_keep": True, "kind": "decision", "retrievable": True, "retained_correctly": False},
                {"unit_id": "g3", "must_keep": True, "kind": "fact", "retrievable": False},
                {"unit_id": "g4", "must_keep": False, "kind": "fact", "retrievable": False},
                {"unit_id": "g5", "must_keep": True, "kind": "judgment", "retrievable": True, "synthesis_target": True, "synthesis_formed": True},
                {"unit_id": "g6", "must_keep": True, "kind": "judgment", "retrievable": False, "synthesis_target": True, "synthesis_formed": False},
            ],
            "retrieval_questions": [
                {"question_id": "q1", "gold_unit_ids": ["g1", "g2"], "retrieved_top_10": ["g2", "g9"]},
                {"question_id": "q2", "gold_unit_ids": ["g5"], "retrieved_top_10": ["g5"]},
            ],
            "why_questions": [
                {"question_id": "w1", "correct": True},
                {"question_id": "w2", "correct": False},
            ],
            "unanswerable_questions": [
                {"question_id": "u1", "fabricated_answer": False},
                {"question_id": "u2", "fabricated_answer": False},
            ],
            "quotes": [
                {"quote_id": "q-1", "exact": True},
                {"quote_id": "q-2", "exact": True},
            ],
        }
        arguments.update(overrides)
        return metrics.evaluate(**arguments)

    def test_evidence_recovery_counts_recovered_over_published(self):
        payload = self._report().as_dict()
        self.assertEqual(payload["metrics"]["evidence_recovery"]["numerator"], 3)
        self.assertEqual(payload["metrics"]["evidence_recovery"]["denominator"], 4)
        self.assertEqual(payload["metrics"]["evidence_recovery"]["verdict"], "fail")

    def test_claim_faithfulness_and_reusable_rate_use_the_published_set(self):
        payload = self._report().as_dict()
        self.assertEqual(payload["metrics"]["claim_faithfulness"]["numerator"], 3)
        self.assertEqual(payload["metrics"]["claim_faithfulness"]["denominator"], 4)
        self.assertEqual(payload["metrics"]["reusable_rate"]["numerator"], 3)
        self.assertEqual(payload["metrics"]["reusable_rate"]["denominator"], 4)
        self.assertEqual(payload["metrics"]["duplication_rate"]["numerator"], 1)

    def test_excluded_rows_leave_the_denominator(self):
        report = self._report(
            published_claims=[
                {"claim_id": "clm_1", "faithful": True, "reusable": True},
                {"claim_id": "clm_2", "faithful": False, "reusable": False, "excluded": True},
            ]
        ).as_dict()
        self.assertEqual(report["metrics"]["claim_faithfulness"]["denominator"], 1)
        self.assertEqual(report["metrics"]["claim_faithfulness"]["value"], 1.0)

    def test_coverage_metrics_split_must_keep_from_decision_units(self):
        payload = self._report().as_dict()
        self.assertEqual(payload["metrics"]["must_keep_coverage"]["numerator"], 3)
        self.assertEqual(payload["metrics"]["must_keep_coverage"]["denominator"], 5)
        self.assertEqual(payload["metrics"]["decision_constraint_coverage"]["numerator"], 1)
        self.assertEqual(payload["metrics"]["decision_constraint_coverage"]["denominator"], 2)

    def test_synthesis_validity_needs_every_premise_and_the_right_modality(self):
        report = self._report(
            published_claims=[
                {
                    "claim_id": "clm_s1",
                    "synthesized": True,
                    "premises_complete": True,
                    "inference_acceptable": True,
                    "modality_correct": True,
                },
                {
                    "claim_id": "clm_s2",
                    "synthesized": True,
                    "premises_complete": True,
                    "inference_acceptable": True,
                    "modality_correct": False,
                },
            ]
        ).as_dict()
        self.assertEqual(report["metrics"]["synthesis_validity"]["numerator"], 1)
        self.assertEqual(report["metrics"]["synthesis_validity"]["denominator"], 2)

    def test_synthesis_coverage_falls_when_nothing_is_synthesized(self):
        report = self._report(
            published_claims=[{"claim_id": "clm_1", "faithful": True, "reusable": True}],
            gold_units=[
                {"unit_id": "g5", "must_keep": True, "synthesis_target": True, "synthesis_formed": False},
                {"unit_id": "g6", "must_keep": True, "synthesis_target": True, "synthesis_formed": False},
            ],
        ).as_dict()
        self.assertEqual(report["metrics"]["synthesis_validity"]["display"], "N/A")
        self.assertEqual(report["metrics"]["synthesis_coverage"]["numerator"], 0)
        self.assertEqual(report["metrics"]["synthesis_coverage"]["denominator"], 2)
        self.assertEqual(report["metrics"]["synthesis_coverage"]["verdict"], "fail")

    def test_recall_at_ten_pools_gold_units_not_questions(self):
        payload = self._report().as_dict()
        self.assertEqual(payload["metrics"]["recall_at_10"]["numerator"], 2)
        self.assertEqual(payload["metrics"]["recall_at_10"]["denominator"], 3)

    def test_recall_counts_a_duplicated_hit_once(self):
        report = self._report(
            retrieval_questions=[
                {"question_id": "q", "gold_unit_ids": ["g1"], "retrieved_top_10": ["g1", "g1"]}
            ]
        ).as_dict()
        self.assertEqual(report["metrics"]["recall_at_10"]["numerator"], 1)
        self.assertEqual(report["metrics"]["recall_at_10"]["denominator"], 1)

    def test_unknown_handling_fails_as_soon_as_one_answer_is_invented(self):
        report = self._report(
            unanswerable_questions=[
                {"question_id": "u1", "fabricated_answer": False},
                {"question_id": "u2", "fabricated_answer": True},
            ]
        ).as_dict()
        self.assertEqual(report["metrics"]["unknown_handling"]["numerator"], 1)
        self.assertEqual(report["metrics"]["unknown_handling"]["verdict"], "fail")

    def test_quote_accuracy_is_measured_against_exact_text(self):
        report = self._report(quotes=[{"quote_id": "q", "exact": False}]).as_dict()
        self.assertEqual(report["metrics"]["quote_accuracy"]["numerator"], 0)
        self.assertEqual(report["metrics"]["quote_accuracy"]["denominator"], 1)
        self.assertEqual(report["metrics"]["quote_accuracy"]["verdict"], "fail")

    def test_a_critical_failure_blocks_release_even_with_every_metric_green(self):
        perfect = {
            "published_evidence": [{"evidence_id": "e", "recovered": True}],
            "published_claims": [{"claim_id": "c", "faithful": True, "reusable": True}],
            "gold_units": [{"unit_id": "g", "must_keep": True, "kind": "decision", "retrievable": True, "retained_correctly": True}],
            "retrieval_questions": [{"question_id": "q", "gold_unit_ids": ["g"], "retrieved_top_10": ["g"]}],
            "why_questions": [{"question_id": "w", "correct": True}],
            "unanswerable_questions": [{"question_id": "u", "fabricated_answer": False}],
            "quotes": [{"quote_id": "q", "exact": True}],
        }
        clean = metrics.evaluate(**perfect).as_dict()
        self.assertEqual(clean["blocking_metrics"], [])
        self.assertEqual(clean["release_ready"], True)

        blocked = metrics.evaluate(
            **perfect, critical_failures=[{"code": "fabricated_adopted_decision", "case_id": "K03"}]
        ).as_dict()
        self.assertEqual(blocked["blocking_metrics"], [])
        self.assertEqual(blocked["release_ready"], False)
        self.assertEqual(blocked["critical_failures"][0]["case_id"], "K03")

    def test_an_unknown_critical_code_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            metrics.evaluate(critical_failures=[{"code": "something_new"}])


class GoldSetTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(
            (REPO_ROOT / "evals" / "knowledge_v2" / "manifest.json").read_text(encoding="utf-8")
        )
        self.units = metrics.load_gold(REPO_ROOT / "evals" / "knowledge_v2" / "gold.jsonl")

    def test_no_gold_unit_is_marked_excluded(self):
        self.assertEqual([], [unit["unit_id"] for unit in self.units if unit.get("excluded")])

    def test_an_excluded_gold_unit_is_refused_rather_than_dropped_from_the_denominator(self):
        polluted = [{**self.units[0], "excluded": True}]
        with self.assertRaises(ValueError):
            metrics.split_counts(polluted, self.manifest)
        with self.assertRaises(ValueError):
            metrics.evaluate(gold_units=polluted)

    def test_the_gold_file_carries_every_declared_field(self):
        for unit in self.units:
            with self.subTest(unit=unit["unit_id"]):
                for field in self.manifest["gold_fields"]:
                    self.assertIn(field, unit)

    def test_constructed_units_are_labelled_as_constructed_and_unconfirmed(self):
        for unit in self.units:
            with self.subTest(unit=unit["unit_id"]):
                self.assertEqual(unit["label_status"], "constructed_unconfirmed")
                self.assertIs(unit["constructed"], True)

    def test_each_group_stays_inside_one_split(self):
        partitions: dict[str, set] = {}
        for unit in self.units:
            partitions.setdefault(unit["group_id"], set()).add(unit["split"])
        for group_id, splits in partitions.items():
            with self.subTest(group=group_id):
                self.assertEqual(len(splits), 1, "a derivation chain must not straddle dev and holdout")

    def test_a_must_keep_unit_names_the_evidence_it_must_be_checked_against(self):
        for unit in self.units:
            if not unit["must_keep"]:
                continue
            with self.subTest(unit=unit["unit_id"]):
                self.assertTrue(
                    unit["evidence_targets"],
                    "a must_keep unit without a target cannot be checked against the material",
                )

    def test_a_negative_example_is_never_also_a_must_keep_unit(self):
        for unit in self.units:
            with self.subTest(unit=unit["unit_id"]):
                self.assertFalse(unit.get("negative_example") and unit["must_keep"])

    def test_forbidding_an_adoption_assertion_requires_the_qualifier_that_prevents_it(self):
        triggers = ("adopt", "采用", "已决定")
        for unit in self.units:
            forbidden = " ".join(unit["forbidden_assertions"])
            if not any(trigger in forbidden for trigger in triggers):
                continue
            with self.subTest(unit=unit["unit_id"]):
                self.assertTrue(
                    unit["required_qualifiers"],
                    "a unit that forbids an adoption claim must say which qualifier keeps it unadopted",
                )

    def test_future_questions_are_counted_so_the_gap_is_not_half_hidden(self):
        summary = metrics.split_counts(self.units, self.manifest)
        self.assertEqual(summary["measured"]["future_questions"], 19)
        self.assertIn("future_questions", summary["shortfalls"])
        self.assertEqual(summary["measured"]["holdout_future_questions"], 8)

    def test_the_shortfall_against_the_spec_minimums_is_reported_not_hidden(self):
        summary = metrics.split_counts(self.units, self.manifest)
        self.assertFalse(summary["meets_minimums"])
        self.assertIn("must_keep", summary["shortfalls"])
        self.assertIn("material_groups", summary["shortfalls"])
        self.assertIn("constructed_unconfirmed", summary["label_statuses"])


if __name__ == "__main__":
    unittest.main()
