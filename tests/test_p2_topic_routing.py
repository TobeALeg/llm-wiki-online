"""Topic routing: a large project reads a catalog, a small one reads its full text.

The six scenarios below are the definition of done. Each runs the real
`SharedWikiService` against a real `SharedWikiStore` with a stub model that records the
payloads it received, so the assertions are about what the model actually saw.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import core  # noqa: E402
from llm_wiki_mcp.core import CoreError, WikiCore, normalize_catalog, plan_catalog_slices, validate_routed_pages  # noqa: E402
from llm_wiki_mcp.shared_service import SharedWikiService  # noqa: E402
from llm_wiki_mcp.store import SharedWikiStore, StoreError  # noqa: E402


def page_body(size):
    return ("body text for this page. " * (size // 25 + 1))[:size]


SOURCE_ID = "conversation:new"


def page(slug, body="Seeded body.", aliases=None, sources=None):
    return {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "type": "guide",
        "status": "current",
        "tags": [],
        "summary": f"Summary for {slug}.",
        "body": body,
        "sources": sources or [f"seed:{slug}"],
        "aliases": aliases or [],
    }


class RecordingModel:
    """A stub model that answers both phases and records every payload it was given."""

    def __init__(self, selections=None, pages=None):
        self.selections = selections or {}
        self.pages = pages
        self.calls = []

    def __call__(self, payload, purpose, pages):
        self.calls.append(payload)
        if payload.get("phase") == "route":
            catalog_slugs = [entry["slug"] for entry in payload["page_catalog"]]
            chosen = [slug for slug in catalog_slugs if slug in self.selections]
            return {"slugs": chosen, "note": "routed"}
        if self.pages is not None:
            return {"pages": self.pages, "note": "merged"}
        return {"pages": [], "note": "merged"}

    @property
    def route_calls(self):
        return [call for call in self.calls if call.get("phase") == "route"]

    @property
    def merge_calls(self):
        return [call for call in self.calls if call.get("phase") != "route"]


class RoutingTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SharedWikiStore(Path(self.temporary.name) / "wiki.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, pages):
        for item in pages:
            self.store.commit_update(
                "seed",
                self.store.current_version(),
                f"seed-{item['slug']}",
                [{"source_id": f"seed:{item['slug']}", "kind": "file", "label": item["slug"], "content": item["body"]}],
                {
                    "schema_version": 1,
                    "pages": [item],
                    "note": "seeded",
                    "source_ids": [f"seed:{item['slug']}"],
                },
            )

    def big_project(self, count=6, body=8_000):
        self.seed([page(f"topic-{index:02d}", body=page_body(body)) for index in range(count)])
        snapshot = core.snapshot_chars(self.store.list_pages()["pages"])
        self.assertGreater(snapshot, core.DIRECT_SUBMIT_MAX_CHARS, "fixture must exceed the threshold")
        return snapshot

    def material(self, content="A new fact about topic 01."):
        return [{"source_id": SOURCE_ID, "kind": "conversation", "label": "note", "content": content}]


class DefinitionOfDoneTests(RoutingTestCase):
    def test_a_small_project_submits_its_full_text_in_one_call(self):
        self.seed([page("alpha"), page("beta")])
        model = RecordingModel(pages=[page("alpha", sources=[SOURCE_ID])])
        service = SharedWikiService(self.store, model)
        service.submit("member-a", self.store.current_version(), "small", self.material(), "Capture")

        self.assertEqual(len(model.calls), 1)
        payload = model.calls[0]
        self.assertNotIn("phase", payload)
        self.assertEqual(
            [entry["slug"] for entry in payload["existing_pages"]],
            ["alpha", "beta"],
        )
        self.assertNotIn("page_catalog", payload)

    def test_crossing_the_threshold_switches_from_one_call_to_two(self):
        self.seed([page("alpha")])
        model = RecordingModel(selections={"alpha"}, pages=[page("alpha", sources=[SOURCE_ID])])
        service = SharedWikiService(self.store, model)

        with mock.patch.object(core, "DIRECT_SUBMIT_MAX_CHARS", 1_000_000):
            service.submit("member-a", self.store.current_version(), "below", self.material(), "Capture")
        self.assertEqual(len(model.calls), 1, "below the threshold the project submits directly")

        model.calls.clear()
        with mock.patch.object(core, "DIRECT_SUBMIT_MAX_CHARS", 1):
            service.submit("member-b", self.store.current_version(), "above", self.material(), "Capture")
        self.assertEqual(len(model.calls), 2, "above the threshold the project routes")
        self.assertEqual(len(model.route_calls), 1)
        self.assertEqual(len(model.merge_calls), 1)

    def test_a_large_project_routes_on_identity_and_merges_only_the_selection(self):
        self.big_project()
        model = RecordingModel(selections={"topic-02", "topic-04"}, pages=[page("topic-02", sources=[SOURCE_ID])])
        SharedWikiService(self.store, model).submit("member-a", self.store.current_version(), "routed", self.material(), "Capture")

        routing, merging = model.route_calls[0], model.merge_calls[0]
        catalog = routing["page_catalog"]
        self.assertEqual(len(catalog), 6)
        self.assertEqual(
            set(catalog[0]),
            {"slug", "title", "type", "summary", "aliases"},
            "the catalog must carry identity only",
        )
        self.assertNotIn("body text for this page", repr(catalog))

        self.assertEqual(
            [entry["slug"] for entry in merging["existing_pages"]],
            ["topic-02", "topic-04"],
            "the merge must read exactly the selected pages",
        )
        self.assertEqual(len(merging["existing_pages"]), 2)

    def test_a_withheld_page_cannot_be_changed_and_the_store_does_not_advance(self):
        self.big_project()
        version_before = self.store.current_version()
        withheld_body = self.store.get_page("topic-05")["page"]["body"]
        # The model selects topic-01 but also rewrites topic-05. The smuggled page cites the
        # submitted material, so it clears the source allowlist and only the routed gate
        # can stop it.
        model = RecordingModel(
            selections={"topic-01"},
            pages=[
                page("topic-01", body="Rewritten.", sources=[SOURCE_ID]),
                page("topic-05", body="Smuggled.", sources=[SOURCE_ID]),
            ],
        )
        service = SharedWikiService(self.store, model)
        with self.assertRaises(CoreError) as error:
            service.submit("member-a", version_before, "smuggle", self.material(), "Capture")
        self.assertIn("topic-05", str(error.exception))
        self.assertEqual(self.store.current_version(), version_before)
        self.assertEqual(self.store.get_page("topic-05")["page"]["body"], withheld_body)

    def test_a_routing_miss_still_lands_rather_than_becoming_a_silent_no_op(self):
        self.big_project()
        version_before = self.store.current_version()
        # Nothing is selected, so the routed merge decides nothing. The fallback full submit
        # is the call that creates the page, and this stub only produces one for a payload
        # carrying the whole snapshot.
        class MissThenCreate:
            def __init__(self):
                self.calls = []

            def __call__(self, payload, purpose, pages):
                self.calls.append(payload)
                if payload.get("phase") == "route":
                    return {"slugs": [], "note": "nothing affected"}
                return {"pages": [page("brand-new", sources=[SOURCE_ID])], "note": "created"}

        model = MissThenCreate()
        SharedWikiService(self.store, model).submit("member-a", version_before, "miss", self.material(), "Capture")

        self.assertEqual(len(model.calls), 2, "routing then one fallback submit")
        self.assertEqual(model.calls[0]["phase"], "route")
        self.assertNotIn("phase", model.calls[1])
        self.assertIn("existing_pages", model.calls[1], "the fallback must carry the full snapshot")
        new_page = self.store.get_page("brand-new")["page"]
        self.assertIsNotNone(new_page, "content must not be dropped when routing selects nothing")
        self.assertEqual(new_page["body"], "Seeded body.")

    def test_a_first_submit_to_an_empty_project_takes_one_call(self):
        model = RecordingModel(pages=[page("first", sources=[SOURCE_ID])])
        service = SharedWikiService(self.store, model)
        # The threshold is patched to zero so page count alone would route. An empty catalog
        # must still produce no routing request and land the content.
        with mock.patch.object(core, "DIRECT_SUBMIT_MAX_CHARS", 0):
            service.submit("member-a", 0, "first", self.material(), "Capture")
        self.assertEqual(model.route_calls, [], "an empty catalog has nothing to route to")
        self.assertEqual(len(model.calls), 1)
        self.assertIn("existing_pages", model.calls[0])


MAX_TEST_BUDGET = 200_000


class RequestShapeTests(unittest.TestCase):
    """The stub model sees the internal payload; this checks the request actually sent."""

    def test_the_routing_request_carries_the_catalog_and_no_page_text(self):
        from llm_wiki_mcp.wiki_prompts import build_request

        request = build_request(
            "Capture",
            {"phase": "route", "materials": [{"source_id": "s", "content": "evidence"}], "page_catalog": []},
            [{"slug": "alpha", "title": "Alpha", "type": "guide", "summary": "S", "aliases": []}],
            phase="route",
        )
        self.assertEqual(request["phase"], "route")
        self.assertNotIn("existing_pages", request)
        self.assertEqual(set(request["output_contract"]), {"slugs", "note"})

    def test_the_merge_request_keeps_the_whole_library_contract(self):
        from llm_wiki_mcp.wiki_prompts import build_request

        request = build_request(
            "Capture",
            {"materials": [{"source_id": "s", "content": "evidence"}]},
            [{"slug": "alpha", "content": "Body", "sources": ["s"]}],
        )
        self.assertNotIn("phase", request)
        self.assertEqual(set(request["output_contract"]), {"pages", "note"})
        self.assertIn("aliases", request["output_contract"]["pages"][0])


class CatalogBudgetTests(unittest.TestCase):
    def catalog(self, count):
        return [{
            "slug": f"page-{index:03d}",
            "title": "T" * 30,
            "type": "guide",
            "summary": "S" * 150,
            "aliases": [],
        } for index in range(count)]

    def test_a_small_catalog_is_one_slice(self):
        self.assertEqual(len(plan_catalog_slices(self.catalog(3), budget=MAX_TEST_BUDGET)), 1)

    def test_an_oversized_catalog_is_split_rather_than_truncated(self):
        entries = normalize_catalog(self.catalog(40))
        slices = plan_catalog_slices(entries, budget=4_000)
        self.assertGreater(len(slices), 1)
        self.assertEqual(
            [entry["slug"] for group in slices for entry in group],
            [entry["slug"] for entry in entries],
            "slicing must lose no page",
        )

    def test_a_single_entry_larger_than_the_budget_is_reported(self):
        with self.assertRaises(CoreError):
            plan_catalog_slices(normalize_catalog(self.catalog(1)), budget=10)

    def test_more_than_five_hundred_pages_reach_routing(self):
        entries = normalize_catalog(self.catalog(520))
        self.assertEqual(len(entries), 520)
        self.assertEqual(sum(len(group) for group in plan_catalog_slices(entries)), 520)


class SelectionValidationTests(unittest.TestCase):
    def test_a_slug_outside_the_slice_is_rejected(self):
        def model(payload, purpose, pages):
            return {"slugs": ["not-in-catalog"], "note": ""}

        with self.assertRaises(CoreError) as error:
            WikiCore(model).select_pages(
                [{"source_id": "s", "content": "c"}],
                [{"slug": "real-page", "title": "T", "type": "guide", "summary": "S"}],
                "Capture",
            )
        self.assertIn("outside the catalog slice", str(error.exception))

    def test_selections_from_separate_slices_are_unioned_without_duplicates(self):
        def model(payload, purpose, pages):
            return {"slugs": [entry["slug"] for entry in payload["page_catalog"]], "note": ""}

        entries = [
            {"slug": f"page-{index}", "title": "T" * 30, "type": "guide", "summary": "S" * 150}
            for index in range(30)
        ]
        with mock.patch.object(core, "MAX_CATALOG_CHARS", 2_000):
            result = WikiCore(model).select_pages([{"source_id": "s", "content": "c"}], entries, "Capture")
        self.assertEqual(len(result["slugs"]), len(set(result["slugs"])))
        self.assertEqual(set(result["slugs"]), {entry["slug"] for entry in entries})

    def test_an_empty_selection_is_allowed(self):
        def model(payload, purpose, pages):
            return {"slugs": [], "note": "nothing affected"}

        result = WikiCore(model).select_pages(
            [{"source_id": "s", "content": "c"}],
            [{"slug": "real-page", "title": "T", "type": "guide", "summary": "S"}],
            "Capture",
        )
        self.assertEqual(result["slugs"], [])

    def test_withheld_pages_must_come_back_unchanged(self):
        with self.assertRaises(CoreError):
            validate_routed_pages(
                [{"slug": "withheld"}, {"slug": "selected"}],
                {"withheld", "selected"},
                {"selected"},
            )

    def test_a_brand_new_slug_is_allowed_through_the_routed_gate(self):
        validate_routed_pages([{"slug": "brand-new"}], {"withheld"}, {"selected"})


if __name__ == "__main__":
    unittest.main()
