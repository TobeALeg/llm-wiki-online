"""A batch failure gets one repeat, and the kind of repeat is counted.

The spec separates a transport retry, which repeats a request that produced no
usable answer, from a semantic repair, which asks again after an answer that did
not satisfy the contract. Only the second can turn a miss into a plausible-looking
wrong answer, so the two are counted apart and each is capped.

The visible effect of the cap is that a second failure stops the batch and leaves
the run unfinished, rather than retrying until it succeeds and reporting a clean
run whose evidence never arrived.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import knowledge_pipeline as pipeline  # noqa: E402
from llm_wiki_mcp.knowledge_types import KnowledgeError  # noqa: E402


class AttemptBudgetTests(unittest.TestCase):
    def test_each_kind_gets_its_own_budget(self):
        budget = pipeline.AttemptBudget()
        self.assertTrue(budget.take("transport_retries"))
        self.assertFalse(budget.take("transport_retries"))
        self.assertEqual(budget.spent["semantic_repairs"], 0)
        self.assertTrue(budget.take("semantic_repairs"))

    def test_the_limit_is_one_per_kind(self):
        self.assertEqual(pipeline.RETRY_LIMITS, {"transport_retries": 1, "semantic_repairs": 1})
        budget = pipeline.AttemptBudget()
        for kind in ("transport_retries", "semantic_repairs"):
            with self.subTest(kind=kind):
                self.assertTrue(budget.take(kind))
                self.assertFalse(budget.take(kind))

    def test_exhaustion_is_reported(self):
        budget = pipeline.AttemptBudget()
        self.assertEqual(budget.exhausted(), [])
        budget.take("transport_retries")
        self.assertEqual(budget.exhausted(), ["transport_retries"])

    def test_an_unknown_kind_is_refused_rather_than_counted(self):
        with self.assertRaises(ValueError):
            pipeline.AttemptBudget().take("best_of_three")

    def test_the_report_keeps_the_two_categories_apart(self):
        budget = pipeline.AttemptBudget()
        budget.take("transport_retries")
        report = pipeline.attempt_report(budget)
        self.assertEqual(report["transport_retries"], 1)
        self.assertEqual(report["semantic_repairs"], 0)
        self.assertIn("plausible wrong answer", report["note"])


class BatchRetryTests(unittest.TestCase):
    def _run(self, failures: int):
        """Run one batch whose discovery role fails a given number of times."""

        calls = {"count": 0}

        def discovery(payload, purpose, pages):
            calls["count"] += 1
            if calls["count"] <= failures:
                raise KnowledgeError("provider returned nothing usable", code="MODEL_UNAVAILABLE")
            return {"candidates": []}

        roles = pipeline.ModelRoles.from_mapping({"discovery": discovery})
        batches = [
            {
                "batch_id": "batch-001",
                "index": 1,
                "chars": 10,
                "source_ids": ["file:a.md"],
                "materials": [
                    {
                        "chunk_id": "chunk-001",
                        "evidence_id": "evd_" + "a" * 64,
                        "text": "Material.",
                    }
                ],
            }
        ]
        return pipeline._extract_batches(
            scope=pipeline.Scope.of("local", "retry"),
            batches=batches,
            purpose="Retry behaviour.",
            roles=roles,
            history_reader=None,
            project_context=(),
        ), calls["count"]

    def test_one_failure_is_repeated_and_the_batch_finishes(self):
        result, calls = self._run(failures=1)
        self.assertEqual(result["unfinished"], [])
        self.assertEqual(result["batches"][0]["status"], "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(result["attempts"]["transport_retries"], 1)
        self.assertEqual(result["attempts"]["semantic_repairs"], 0)

    def test_a_second_failure_stops_the_batch_instead_of_retrying_forever(self):
        result, calls = self._run(failures=99)
        self.assertEqual(calls, 2, "the cap is one repeat, so the model is asked at most twice")
        self.assertEqual(result["unfinished"], ["batch-001"])
        self.assertEqual(result["batches"][0]["status"], "unfinished")
        self.assertEqual(result["batches"][0]["error_code"], "MODEL_UNAVAILABLE")
        self.assertIn("provider returned nothing usable", result["batches"][0]["error"])
        self.assertEqual(result["attempts"]["transport_retries"], 1)
        self.assertEqual(pipeline.run_status(result), "failed")

    def test_a_clean_batch_spends_nothing(self):
        result, calls = self._run(failures=0)
        self.assertEqual(calls, 1)
        self.assertEqual(result["attempts"]["transport_retries"], 0)
        self.assertEqual(result["attempts"]["semantic_repairs"], 0)


if __name__ == "__main__":
    unittest.main()
