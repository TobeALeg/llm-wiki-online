import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.core import (  # noqa: E402
    DIRECT,
    DIRECT_SUBMIT_MAX_CHARS,
    MAX_CATALOG_CHARS,
    MAX_EXISTING_CHARS,
    MAX_EXISTING_PAGES,
    MAX_MATERIAL_CHARS,
    MAX_MATERIAL_CONTENT_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_PAGE_BODY_CHARS,
    MAX_ROUTE_OUTPUT_CHARS,
    MODEL_CONTEXT_TOKENS,
    ROUTED,
    CoreError,
    WikiCore,
    normalize_material,
    submit_mode,
)


class WikiCoreTests(unittest.TestCase):
    def test_returns_structured_update_with_traceable_sources(self):
        def model(payload, purpose, pages):
            self.assertEqual(purpose, "Keep decisions")
            self.assertEqual(payload["materials"][0]["source_id"], "file:architecture.md@sha256:abc123")
            return {
                "pages": [{
                    "slug": "storage-decision",
                    "title": "Storage decision",
                    "type": "decision",
                    "status": "current",
                    "tags": ["storage"],
                    "summary": "SQLite is the initial store.",
                    "body": "The first implementation uses SQLite.",
                    "sources": ["file:architecture.md@sha256:abc123"],
                }],
                "note": "Recorded the decision.",
            }

        package = WikiCore(model).organize(
            [{"source_id": "file:architecture.md@sha256:abc123", "kind": "file", "content": "SQLite"}],
            [],
            "Keep decisions",
        )

        self.assertEqual(package["schema_version"], 1)
        self.assertEqual(package["pages"][0]["slug"], "storage-decision")
        self.assertEqual(package["pages"][0]["sources"], ["file:architecture.md@sha256:abc123"])

    def test_rejects_paths_before_model_is_called(self):
        called = False

        def model(*args):
            nonlocal called
            called = True
            return {"pages": []}

        with self.assertRaises(CoreError):
            WikiCore(model).organize(
                [{"source_id": "file:x", "content": "safe", "path": "/etc/passwd"}],
                [],
                "purpose",
            )
        self.assertFalse(called)

    def test_rejects_unknown_source_from_model(self):
        def model(payload, purpose, pages):
            return {"pages": [{
                "slug": "bad",
                "title": "Bad",
                "type": "guide",
                "status": "current",
                "tags": [],
                "summary": "bad",
                "body": "bad",
                "sources": ["file:not-submitted@sha256:nope"],
            }]}

        with self.assertRaisesRegex(CoreError, "unknown sources"):
            WikiCore(model).organize(
                [{"source_id": "file:known@sha256:abc123", "content": "safe"}],
                [],
                "purpose",
            )

    def test_update_source_ids_only_include_sources_cited_by_returned_pages(self):
        def model(payload, purpose, pages):
            return {"pages": [{
                "slug": "new",
                "title": "New",
                "type": "guide",
                "status": "current",
                "tags": [],
                "summary": "New",
                "body": "New",
                "sources": ["conversation:new"],
            }]}

        result = WikiCore(model).organize(
            [{"source_id": "conversation:new", "content": "new"}],
            [{"slug": "old", "body": "old", "sources": ["conversation:old"]}],
            "Capture",
        )
        self.assertEqual(result["source_ids"], ["conversation:new"])


class BudgetInvariantTests(unittest.TestCase):
    def test_request_budgets_stay_under_the_model_context_window(self):
        # One submit sends the materials, the whole project snapshot and reserves room for
        # the model's response. Exceeding the window is not a clean rejection: the provider
        # answer surfaces as a generic model failure, so the caps must sum under the window.
        self.assertLessEqual(
            MAX_MATERIAL_CHARS + MAX_EXISTING_CHARS + MAX_OUTPUT_CHARS,
            MODEL_CONTEXT_TOKENS,
        )

    def test_every_routed_request_stays_under_the_model_context_window(self):
        # The routed path makes two calls. Each one must fit the window on its own; summing
        # both would be wrong, since they are sequential requests, not one payload.
        routing_call = MAX_MATERIAL_CHARS + MAX_CATALOG_CHARS + MAX_ROUTE_OUTPUT_CHARS
        merging_call = MAX_MATERIAL_CHARS + MAX_EXISTING_CHARS + MAX_OUTPUT_CHARS
        for name, budget in (("routing", routing_call), ("merging", merging_call)):
            with self.subTest(phase=name):
                self.assertLessEqual(budget, MODEL_CONTEXT_TOKENS)

    def test_the_material_limit_is_not_silently_the_page_body_limit(self):
        self.assertEqual(MAX_MATERIAL_CONTENT_CHARS, MAX_PAGE_BODY_CHARS)
        with self.assertRaisesRegex(CoreError, "100000 character limit"):
            normalize_material({"source_id": "s", "content": "x" * (MAX_MATERIAL_CONTENT_CHARS + 1)})


class SubmitModeTests(unittest.TestCase):
    def test_a_small_project_submits_its_full_text(self):
        self.assertEqual(submit_mode(3, 1_000), DIRECT)
        self.assertEqual(submit_mode(0, 0), DIRECT)

    def test_the_size_trigger_is_the_boundary_itself(self):
        self.assertEqual(submit_mode(1, DIRECT_SUBMIT_MAX_CHARS), DIRECT)
        self.assertEqual(submit_mode(1, DIRECT_SUBMIT_MAX_CHARS + 1), ROUTED)

    def test_a_project_too_large_to_submit_directly_routes(self):
        # Past MAX_EXISTING_PAGES a direct submit cannot run, so routing is the only option
        # even though the pages themselves are tiny.
        self.assertEqual(submit_mode(MAX_EXISTING_PAGES + 1, 10), ROUTED)

    def test_many_short_pages_do_not_route_on_page_count_alone(self):
        # A catalog entry costs about 300 characters of identity while a body can be one
        # character long, so page count is not a cost trigger.
        self.assertEqual(submit_mode(MAX_EXISTING_PAGES, 200), DIRECT)


if __name__ == "__main__":
    unittest.main()
