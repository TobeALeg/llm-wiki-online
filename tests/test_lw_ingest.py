import importlib.util
import re
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "skills" / "lw" / "scripts" / "wiki.py"
SPEC = importlib.util.spec_from_file_location("llm_wiki_ingest", SCRIPT)
wiki = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(wiki)


ONE_FILE_BATCH_CHARS = 40


def args(episode=None, episode_file=None):
    return type("Args", (), {"episode": episode, "episode_file": episode_file})()


def long_document(paragraphs=40, width=1_000):
    blocks = [
        f"Paragraph {number}: " + f"word{number} " * (width // 8) for number in range(paragraphs)
    ]
    blocks.append("Tail: the delivery date is 2026-09-30.")
    return "\n\n".join(blocks) + "\n"


UPDATED_AT = re.compile(r"^updated_at: .*$", re.M)


def knowledge_of(text):
    """A page's content without the time it was written.

    `updated_at` is a second-resolution stamp, so two otherwise identical renders can
    differ in it. Dropping that one line is what lets a test compare what a page says.
    """

    return UPDATED_AT.sub("updated_at: <time>", text)


def page(slug, source, marker="recording"):
    return {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "type": "decision",
        "status": "current",
        "tags": ["test"],
        "summary": f"{marker} summary for {slug}",
        "body": f"{marker} body for {slug}.",
        "sources": [source],
    }


def answering_model(calls, slug="delivery-plan"):
    def model(payload, purpose, pages):
        calls.append(payload)
        sources = sorted({unit["source_id"] for unit in payload["chunks"]})
        if not sources:
            return {"pages": [], "note": "no chunk evidence in this batch"}
        return {"pages": [page(slug, sources[0])], "note": "recorded"}

    return model


def accumulating_model(calls, slug="delivery-plan"):
    """A model that folds the sources it is shown into one page, so evidence accumulates."""

    def model(payload, purpose, pages):
        calls.append(payload)
        sources = set()
        for entry in pages:
            sources.update(entry.get("sources", []))
        sources.update(unit["source_id"] for unit in payload["chunks"])
        return {
            "pages": [{
                "slug": slug,
                "title": "Delivery plan",
                "type": "decision",
                "status": "current",
                "tags": ["delivery"],
                "summary": "Delivery dates.",
                "body": "Consolidated delivery dates.",
                "sources": sorted(sources),
            }],
            "note": "recorded",
        }

    return model


class WikiIngestTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        wiki.init_wiki(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def pages_dir(self):
        return self.root / ".llm-wiki" / "pages"

    @property
    def runs_dir(self):
        return self.root / ".llm-wiki" / "runs"

    def state_file(self):
        return self.root / ".llm-wiki" / "state.json"

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def rebuild(self):
        state = wiki.load_state(self.root)
        return state, wiki.scan_records(self.root)


class StateMigrationTests(WikiIngestTestCase):
    def test_a_version_one_state_is_upgraded_and_files_need_rechecking(self):
        """The old format recorded a hash for files it had truncated or skipped."""

        self.write("notes.md", "A decision.\n")
        legacy = {
            "version": 1,
            "files": {"notes.md": {"sha256": "deadbeef", "bytes": 12}},
            "processed_episodes": ["2026-01-01-abc"],
            "pages": {},
            "last_update": None,
            "provider": {},
        }
        self.state_file().write_text(json.dumps(legacy), encoding="utf-8")

        state = wiki.load_state(self.root)

        self.assertEqual(state["version"], wiki.STATE_VERSION)
        self.assertEqual(state["processed_episodes"], ["2026-01-01-abc"])
        record = state["files"]["notes.md"]
        self.assertEqual(record["sha256"], "deadbeef")
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(record["reason"])
        self.assertEqual(
            json.loads(self.state_file().read_text(encoding="utf-8"))["version"],
            wiki.STATE_VERSION,
            "the upgrade is persisted so the next run does not redo it",
        )

    def test_an_unknown_state_version_is_still_refused(self):
        self.state_file().write_text(json.dumps({"version": 99}), encoding="utf-8")
        with self.assertRaises(wiki.WikiError):
            wiki.load_state(self.root)

    def test_the_upgrade_keeps_a_copy_of_the_previous_state_file(self):
        self.write("notes.md", "A decision.\n")
        legacy = {"version": 1, "files": {"notes.md": {"sha256": "old", "bytes": 12}}, "pages": {}}
        self.state_file().write_text(json.dumps(legacy), encoding="utf-8")

        wiki.load_state(self.root)

        backup = self.root / ".llm-wiki" / "state.v1.json"
        self.assertTrue(backup.is_file(), "the rewritten state needs a copy to compare against")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), legacy)

    def test_a_malformed_state_file_raises_a_wiki_error_not_a_crash(self):
        for broken in ({"version": 1, "files": []}, {"version": 1, "files": {"a.md": "nope"}}):
            with self.subTest(broken=broken):
                self.state_file().write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(wiki.WikiError):
                    wiki.load_state(self.root)

    def test_unverified_files_are_re_ingested_then_become_complete(self):
        self.write("notes.md", "A decision.\n")
        legacy = {
            "version": 1,
            "files": {"notes.md": {"sha256": "not-the-real-hash", "bytes": 12}},
            "processed_episodes": [],
            "pages": {},
            "last_update": None,
            "provider": {},
        }
        self.state_file().write_text(json.dumps(legacy), encoding="utf-8")
        calls = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
            wiki.do_update(self.root, args())
        self.assertEqual(len(calls), 1, "the unverified file is re-processed once")
        state = wiki.load_state(self.root)
        self.assertEqual(state["files"]["notes.md"]["status"], "complete")
        self.assertTrue(self.pages_dir.joinpath("delivery-plan.md").is_file())


class SkipReportingTests(WikiIngestTestCase):
    def test_an_oversized_source_is_reported_with_its_reason(self):
        """It must not vanish silently the way a skipped file does today."""

        self.write("notes.md", "small\n")
        self.write("huge.md", "z" * (wiki.MAX_FILE_BYTES + 1))

        skips = wiki.scan_skips(self.root)
        by_path = {entry["path"]: entry["reason"] for entry in skips}
        self.assertIn("huge.md", by_path)
        self.assertIn(str(wiki.MAX_FILE_BYTES), by_path["huge.md"])
        self.assertNotIn("huge.md", wiki.scan_records(self.root))

    def test_an_undecodable_source_is_reported(self):
        self.write("notes.md", "small\n")
        (self.root / "blob.md").write_bytes(b"\xff\xfe\x00binary")

        by_path = {entry["path"]: entry["reason"] for entry in wiki.scan_skips(self.root)}
        self.assertIn("blob.md", by_path)
        self.assertTrue(by_path["blob.md"])

    def test_status_surfaces_the_skip_reason(self):
        self.write("huge.md", "z" * (wiki.MAX_FILE_BYTES + 1))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(wiki.main(["status", "--root", str(self.root)]), 0)
        self.assertIn("huge.md", output.getvalue())

    def test_update_surfaces_the_skip_reason_instead_of_silently_ignoring_it(self):
        self.write("small.md", "A fact.\n")
        self.write("huge.md", "z" * (wiki.MAX_FILE_BYTES + 1))
        calls = []
        output = StringIO()
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
            with redirect_stdout(output):
                wiki.do_update(self.root, args())
        rendered = output.getvalue()
        self.assertIn("skipped huge.md", rendered)
        self.assertIn(str(wiki.MAX_FILE_BYTES), rendered)


class InvalidModelOutputTests(WikiIngestTestCase):
    """The manifest must never hold a page the run cannot commit.

    A batch whose output fails validation is not finished work. Recording it as
    finished makes the retry skip the model, re-check the same bad page, and fail
    again forever, with no request ever reaching the provider.
    """

    def bad_page(self, source):
        return {
            "slug": "delivery-plan",
            "title": "Delivery plan",
            "type": "decision",
            "status": "current",
            "tags": ["delivery"],
            "summary": "Delivery dates.",
            "body": "Consolidated delivery dates.",
            "sources": [source],
        }

    def test_an_invalid_page_does_not_wedge_the_run(self):
        self.write("notes.md", "A decision.\n")
        ghost = self.bad_page("file:ghost.md@sha256:deadbeef0000")

        with mock.patch.object(wiki, "call_model", return_value={"pages": [ghost], "note": ""}):
            with self.assertRaises(wiki.WikiError):
                wiki.do_update(self.root, args())

        self.assertEqual(list(self.pages_dir.glob("*.md")), [], "nothing invalid is committed")

        calls = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
            wiki.do_update(self.root, args())

        self.assertTrue(calls, "the retry asks the model again instead of replaying the bad draft")
        self.assertIsNone(wiki.load_run(self.root), "the run converges")
        self.assertEqual(len(list(self.pages_dir.glob("*.md"))), 1)
        self.assertEqual(wiki.load_state(self.root)["files"]["notes.md"]["status"], "complete")

    def test_a_slug_the_filesystem_cannot_hold_is_rejected_before_any_progress(self):
        """Found by review: a 253-character slug passed validation and failed at write time."""

        self.write("notes.md", "A decision.\n")
        real_source = wiki.plan_ingest(
            wiki.load_state(self.root), wiki.scan_records(self.root)
        )["files"][0]["source_id"]
        huge = self.bad_page(real_source)
        huge["slug"] = "a" * 253

        with mock.patch.object(wiki, "call_model", return_value={"pages": [huge], "note": ""}):
            with self.assertRaises(wiki.WikiError) as caught:
                wiki.do_update(self.root, args())
        self.assertIn("slug", str(caught.exception).lower(), "the slug is what is rejected")

        self.assertEqual(list(self.pages_dir.glob("*.md")), [], "nothing is written")
        recorded = wiki.load_run(self.root)
        self.assertIsNotNone(recorded, "the run stays open so a retry can finish it")
        self.assertEqual(
            [unit["chunk_id"] for unit in recorded["units"] if unit.get("done")],
            [],
            "the rejected batch made no progress",
        )

        calls = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
            wiki.do_update(self.root, args())

        self.assertTrue(calls, "the retry asks the model again instead of replaying the bad draft")
        self.assertIsNone(wiki.load_run(self.root), "the run converges once the model behaves")

    def test_a_deterministic_commit_failure_does_not_wedge_at_zero_requests(self):
        """The run must keep asking the model, never spin on stored drafts with no request.

        A publish step that fails every time (here, the atomic rename) must not leave the
        run replaying its stored drafts: the retry has to go back to the model.
        """

        self.write("notes.md", "A decision.\n")
        real_replace = Path.replace

        def failing_replace(self, target, *args, **kwargs):
            if str(target).endswith("delivery-plan.md"):
                raise OSError(28, "No space left on device")
            return real_replace(self, target, *args, **kwargs)

        calls = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
            with mock.patch.object(Path, "replace", failing_replace):
                with self.assertRaises(OSError):
                    wiki.do_update(self.root, args())
        self.assertGreater(len(calls), 0, "the first attempt does reach the model")

        retry = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(retry)):
            with mock.patch.object(Path, "replace", failing_replace):
                with self.assertRaises(OSError):
                    wiki.do_update(self.root, args())

        self.assertTrue(
            retry,
            "the retry must ask the model again; zero requests here is the wedge",
        )
        recorded = wiki.load_run(self.root)
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded["drafted_pages"], [], "unusable drafts are not kept")

    def test_a_non_object_model_response_raises_a_wiki_error(self):
        """The check lives inside call_model, so it needs a real HTTP response to exercise."""

        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": '["not", "an", "object"]'}}]}
        ).encode("utf-8")
        response.__enter__ = lambda self: response
        response.__exit__ = lambda *args: False

        with mock.patch.dict(os.environ, {"LLM_WIKI_API_KEY": "test-key"}):
            with mock.patch("urllib.request.urlopen", return_value=response):
                with self.assertRaises(wiki.WikiError) as caught:
                    wiki.call_model({"chunks": []}, "purpose", [])
        self.assertIn("object", str(caught.exception).lower())

    def test_a_failed_page_write_leaves_no_page_on_disk(self):
        """A partial multi-page commit must not leave an uncataloged page behind, and the
        next attempt must ask the model again rather than replay the unusable drafts."""

        self.write("a.md", "First decision.\n")
        self.write("b.md", "Second decision.\n")

        def two_pages(payload, purpose, pages):
            sources = sorted({unit["source_id"] for unit in payload["chunks"]})
            if not sources:
                return {"pages": [], "note": ""}
            return {
                "pages": [
                    page("first-topic", sources[0]),
                    page("second-topic", sources[0]),
                ],
                "note": "two pages",
            }

        real_write = Path.write_text

        def failing_write(self, data, *args, **kwargs):
            if "second-topic" in str(self):
                raise OSError(28, "No space left on device")
            return real_write(self, data, *args, **kwargs)

        with mock.patch.object(wiki, "call_model", side_effect=two_pages):
            with mock.patch.object(Path, "write_text", failing_write):
                with self.assertRaises(OSError):
                    wiki.do_update(self.root, args())

        self.assertEqual(
            list(self.pages_dir.glob("*.md")), [], "a half-written commit publishes nothing"
        )

        # The unusable drafts are gone, so a healthy retry redoes the model work.
        retry = []
        with mock.patch.object(wiki, "call_model", side_effect=two_pages):
            wiki.do_update(self.root, args())
        self.assertEqual(wiki.load_state(self.root)["files"]["a.md"]["status"], "complete")
        self.assertEqual(len(list(self.pages_dir.glob("*.md"))), 2, "both pages land")
        self.assertIsNone(wiki.load_run(self.root))

    def test_a_batch_that_fails_validation_is_not_recorded_as_done(self):
        self.write("notes.md", long_document(paragraphs=20))
        state, records = self.rebuild()
        run = wiki.create_run(self.root, wiki.plan_ingest(state, records))

        ghost = self.bad_page("file:ghost.md@sha256:deadbeef0000")
        with mock.patch.object(wiki, "call_model", return_value={"pages": [ghost], "note": ""}):
            with self.assertRaises(wiki.WikiError):
                wiki.do_update(self.root, args())

        recorded = wiki.load_run(self.root)
        self.assertIsNotNone(recorded, "the run stays open for a retry")
        self.assertEqual(
            [unit["chunk_id"] for unit in recorded["units"] if unit.get("done")],
            [],
            "a rejected batch leaves no chunk marked done",
        )
        self.assertEqual(recorded.get("drafted_pages", []), [], "a rejected page is not stored")


class PlanningTests(WikiIngestTestCase):
    def test_an_unchanged_complete_source_is_not_planned_again(self):
        self.write("notes.md", "A decision.\n")
        calls = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
            wiki.do_update(self.root, args())
        self.assertEqual(len(calls), 1)

        state, records = self.rebuild()
        plan = wiki.plan_ingest(state, records)
        self.assertEqual(plan["chunks_total"], 0)
        self.assertEqual(plan["files"], [])

    def test_a_changed_source_is_planned_again(self):
        self.write("notes.md", "A decision.\n")
        with mock.patch.object(wiki, "call_model", side_effect=answering_model([])):
            wiki.do_update(self.root, args())
        self.write("notes.md", "A different decision.\n")

        state, records = self.rebuild()
        plan = wiki.plan_ingest(state, records)
        self.assertEqual([entry["path"] for entry in plan["files"]], ["notes.md"])
        self.assertEqual(plan["chunks_total"], 1)

    def test_the_whole_long_source_is_covered_not_the_first_excerpt(self):
        text = long_document()
        self.write("notes.md", text)

        state, records = self.rebuild()
        plan = wiki.plan_ingest(state, records)

        entry = plan["files"][0]
        self.assertGreater(len(entry["chunks"]), 1)
        self.assertGreater(max(chunk["end"] for chunk in entry["chunks"]), 24_000)
        self.assertIn("the delivery date is 2026-09-30", entry["chunks"][-1]["text"])
        self.assertEqual(entry["sha256"], records["notes.md"]["sha256"])
        self.assertEqual(entry["revision_id"], f"sha256:{records['notes.md']['sha256']}")
        self.assertTrue(entry["parse_id"].startswith("sha256:"))

    def test_re_chunking_the_same_source_gives_the_same_parse_id(self):
        self.write("notes.md", long_document(paragraphs=8))
        state, records = self.rebuild()
        first = wiki.plan_ingest(state, records)["files"][0]["parse_id"]
        second = wiki.plan_ingest(state, records)["files"][0]["parse_id"]
        self.assertEqual(first, second)


class BatchTests(WikiIngestTestCase):
    def build_run(self, paragraphs=40, budget=None):
        self.write("notes.md", long_document(paragraphs=paragraphs))
        state, records = self.rebuild()
        plan = wiki.plan_ingest(state, records)
        return wiki.create_run(self.root, plan)

    def test_batches_partition_every_unit_exactly_once(self):
        run = self.build_run()
        batches = wiki.prepare_batches(run, 20_000)
        self.assertGreater(len(batches), 1)
        packed = [unit["chunk_id"] for batch in batches for unit in batch]
        self.assertEqual(packed, [unit["chunk_id"] for unit in run["units"]])
        self.assertEqual(len(set(packed)), len(packed))

    def test_a_starved_budget_still_makes_progress_one_unit_at_a_time(self):
        run = self.build_run()
        batches = wiki.prepare_batches(run, 0)
        self.assertTrue(all(len(batch) >= 1 for batch in batches))
        self.assertEqual(sum(len(batch) for batch in batches), len(run["units"]))

    def test_a_manifest_keeps_its_identity_across_a_reload(self):
        run = self.build_run()
        self.assertEqual(wiki.load_run(self.root)["run_id"], run["run_id"])

    def test_the_run_is_not_visible_before_it_is_created_and_gone_after_commit(self):
        self.assertIsNone(wiki.load_run(self.root))
        run = self.build_run()
        self.assertIsNotNone(wiki.load_run(self.root))
        state = wiki.load_state(self.root)
        wiki.commit_run(self.root, run)
        self.assertIsNone(wiki.load_run(self.root))


class RunReportTests(WikiIngestTestCase):
    def test_report_separates_complete_partial_and_deferred_files(self):
        self.write("a.md", long_document(paragraphs=20))
        self.write("b.md", long_document(paragraphs=20))
        state, records = self.rebuild()
        run = wiki.create_run(self.root, wiki.plan_ingest(state, records))

        report = wiki.run_report(run)
        self.assertEqual({entry["status"] for entry in report["files"].values()}, {"deferred"})

        first = wiki.prepare_batches(run, 20_000)[0]
        first[0]["done"] = True
        report = wiki.run_report(run)
        statuses = {entry["status"] for entry in report["files"].values()}
        self.assertEqual(statuses, {"partial", "deferred"})
        partial = next(entry for entry in report["files"].values() if entry["status"] == "partial")
        self.assertGreater(partial["chunks_done"], 0)
        self.assertLess(partial["chunks_done"], partial["chunks_total"])


class FailureAndResumeTests(WikiIngestTestCase):
    def setUp(self):
        super().setUp()
        self.write("notes.md", long_document(paragraphs=30))

    def test_a_failed_batch_leaves_no_pages_and_an_open_manifest(self):
        calls = []

        def flaky(payload, purpose, pages):
            calls.append(payload)
            if len(calls) == 2:
                raise wiki.WikiError("model unavailable")
            return answering_model([])(payload, purpose, pages)

        with mock.patch.object(wiki, "material_budget", return_value=20_000):
            with mock.patch.object(wiki, "call_model", side_effect=flaky):
                with self.assertRaises(wiki.WikiError):
                    wiki.do_update(self.root, args())

        self.assertEqual(list(self.pages_dir.glob("*.md")), [], "nothing is committed on failure")
        run = wiki.load_run(self.root)
        self.assertIsNotNone(run, "the unfinished run stays on disk so it can be replayed")
        done = [unit["chunk_id"] for unit in run["units"] if unit["done"]]
        self.assertEqual(done, [unit["chunk_id"] for unit in calls[0]["chunks"]])
        state = wiki.load_state(self.root)
        self.assertNotIn("notes.md", state["files"], "a failed run never marks a source complete")

    def test_the_retry_skips_finished_units_and_does_not_duplicate_pages(self):
        calls = []

        def flaky(payload, purpose, pages):
            calls.append(payload)
            if len(calls) == 2:
                raise wiki.WikiError("model unavailable")
            return answering_model([])(payload, purpose, pages)

        with mock.patch.object(wiki, "material_budget", return_value=20_000):
            with mock.patch.object(wiki, "call_model", side_effect=flaky):
                with self.assertRaises(wiki.WikiError):
                    wiki.do_update(self.root, args())
        finished = [unit["chunk_id"] for unit in calls[0]["chunks"]]

        retry = []
        with mock.patch.object(wiki, "material_budget", return_value=20_000):
            with mock.patch.object(wiki, "call_model", side_effect=answering_model(retry)):
                wiki.do_update(self.root, args())

        resubmitted = [unit["chunk_id"] for payload in retry for unit in payload["chunks"]]
        self.assertFalse(set(finished) & set(resubmitted), "finished chunks are not sent twice")
        self.assertTrue(resubmitted, "the unfinished remainder is sent")
        self.assertIsNone(wiki.load_run(self.root))
        self.assertEqual(len(list(self.pages_dir.glob("*.md"))), 1, "no duplicate page is created")
        state = wiki.load_state(self.root)
        record = state["files"]["notes.md"]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(len(record["chunks_done"]), record["chunks_total"])
        self.assertEqual(record["chunks_total"], len(finished) + len(resubmitted))

    def test_a_source_edited_during_an_open_run_aborts_instead_of_mixing_revisions(self):
        state, records = self.rebuild()
        run = wiki.create_run(self.root, wiki.plan_ingest(state, records))
        self.write("notes.md", "Replaced while the run was open.\n")
        with self.assertRaises(wiki.WikiError):
            wiki.unit_text(self.root, run["units"][0])

    def test_a_source_edited_during_a_run_is_abandoned_and_the_run_still_finishes(self):
        """A moved source must not wedge every later retry against the same run."""

        self.write("keeper.md", "Keeper fact.\n")
        self.write("mover.md", long_document(paragraphs=20))
        state, records = self.rebuild()
        run = wiki.create_run(self.root, wiki.plan_ingest(state, records))
        self.assertTrue([unit for unit in run["units"] if unit["path"] == "mover.md"])

        self.write("mover.md", "Something else entirely.\n")
        dropped = wiki.drop_drifted_sources(self.root, run, list(run["units"]))

        self.assertEqual(dropped, ["mover.md"])
        self.assertNotIn("mover.md", {unit["path"] for unit in run["units"]})
        self.assertNotIn("mover.md", run["files"])
        self.assertIn("keeper.md", {unit["path"] for unit in run["units"]})

        calls = []
        with mock.patch.object(wiki, "material_budget", return_value=20):
            with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
                wiki.do_update(self.root, args())

        self.assertIsNone(wiki.load_run(self.root), "the run converges instead of sticking")
        state = wiki.load_state(self.root)
        self.assertEqual(state["files"]["keeper.md"]["status"], "complete")
        self.assertNotIn("mover.md", state["files"], "an abandoned source is not marked complete")
        self.assertIn("mover.md", wiki.scan_records(self.root), "it stays live for the next run")

    def test_an_abandoned_source_still_lets_the_drafted_page_commit(self):
        """The cross-process case: a draft citing the old revision must survive the retry.

        The reviewer found that when the run is resumed in a NEW process, rescanning sees
        the edited file's new hash, so a draft the crashed run already produced names a
        source id that no longer looks known. That rejected the whole run on every retry.
        """

        self.write("a.md", long_document(paragraphs=20))
        self.write("b.md", "Edited during the run.\n")
        state, records = self.rebuild()
        run = wiki.create_run(self.root, wiki.plan_ingest(state, records))
        self.assertTrue([u for u in run["units"] if u["path"] == "b.md"])

        # A batch runs and drafts a page citing b.md's original source id. b.md's own units
        # are still unfinished when the run stops, then the source is edited on disk.
        old_source = run["files"]["b.md"]["source_id"]
        run["drafted_pages"] = [page("delivery-plan", old_source, marker="drafted")]
        wiki.save_run(self.root, run)
        self.write("b.md", "Changed content entirely.\n")

        fresh_state = wiki.load_state(self.root)
        fresh_records = wiki.scan_records(self.root)
        remaining = [u for u in run["units"] if not u.get("done")]
        drifted = wiki.drop_drifted_sources(self.root, run, remaining)
        self.assertEqual(drifted, ["b.md"], "the edited source is detected once rescanned")
        wiki.save_run(self.root, run)

        committed = wiki.commit_update(
            self.root, fresh_state, run, fresh_records, run["drafted_pages"], [], ["resumed"], {}
        )

        self.assertEqual(committed, ["delivery-plan"])
        rendered = (self.pages_dir / "delivery-plan.md").read_text(encoding="utf-8")
        self.assertIn(old_source, rendered, "the draft keeps the revision the model actually read")
        self.assertIsNone(wiki.load_run(self.root), "the run converges instead of sticking")

    def test_two_manifests_resolve_to_the_newest_not_an_arbitrary_one(self):
        self.write("xyz.md", long_document(paragraphs=20))
        state, records = self.rebuild()
        newest = wiki.create_run(self.root, wiki.plan_ingest(state, records))
        stale = wiki.runs_dir(self.root) / "run-0000000000000000.json"
        stale.write_text(json.dumps({**newest, "run_id": "run-0000000000000000"}), encoding="utf-8")
        # Make the lexicographically-first name genuinely the older file.
        older = stale.stat().st_mtime - 600
        os.utime(stale, (older, older))

        chosen = wiki.load_run(self.root)

        self.assertEqual(chosen["run_id"], newest["run_id"])

    def test_committing_a_run_clears_every_manifest(self):
        """A leftover manifest would otherwise replay forever and resend paid-for work."""

        self.write("xyz.md", long_document(paragraphs=20))
        state, records = self.rebuild()
        run = wiki.create_run(self.root, wiki.plan_ingest(state, records))
        stale = wiki.runs_dir(self.root) / "run-0000000000000000.json"
        stale.write_text(json.dumps({**run, "run_id": "run-0000000000000000"}), encoding="utf-8")

        wiki.commit_run(self.root, run)

        self.assertEqual(list(wiki.runs_dir(self.root).glob("*.json")), [])
        self.assertIsNone(wiki.load_run(self.root))

    def test_an_unreadable_manifest_is_set_aside_and_the_next_run_proceeds(self):
        """Found by review: external corruption of a manifest stalled every later update."""

        broken_cases = {
            "not json at all": "{ this is not json",
            "missing units": json.dumps({"run_id": "run-x", "files": {}}),
            "units not a list": json.dumps({"run_id": "run-x", "files": {}, "units": {}}),
            "files not a dict": json.dumps({"run_id": "run-x", "files": [], "units": []}),
            "missing run_id": json.dumps({"files": {}, "units": []}),
        }

        for label, content in broken_cases.items():
            with self.subTest(broken=label):
                runs = wiki.runs_dir(self.root)
                runs.mkdir(parents=True, exist_ok=True)
                for existing in runs.glob("*"):
                    existing.unlink()
                (runs / "run-broken.json").write_text(content, encoding="utf-8")

                self.assertIsNone(wiki.load_run(self.root), f"{label} is not a usable run")
                self.assertEqual(
                    list(runs.glob("*.json")), [], f"{label} is moved out of the way"
                )
                self.assertTrue(list(runs.glob("*.broken")), f"{label} is kept for inspection")

                self.write("notes.md", f"Content for {label}.\n")
                calls = []
                with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
                    wiki.do_update(self.root, args())

                self.assertTrue(calls, f"{label} must not stop the update")
                self.assertEqual(len(list(self.pages_dir.glob("*.md"))), 1)


class ModelPayloadTests(WikiIngestTestCase):
    def test_the_batch_carries_chunk_text_offsets_and_a_short_handle(self):
        self.write("notes.md", long_document())
        calls = []
        with mock.patch.object(wiki, "material_budget", return_value=20_000):
            with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)):
                wiki.do_update(self.root, args())

        seen = [unit for payload in calls for unit in payload["chunks"]]
        self.assertIn("the delivery date is 2026-09-30", "\n".join(unit["text"] for unit in seen))
        for unit in seen:
            self.assertTrue(unit["handle"])
            self.assertTrue(unit["chunk_id"].startswith("chunk-"))
            self.assertLess(unit["start"], unit["end"])
            self.assertEqual(unit["source_id"], seen[0]["source_id"])
        self.assertEqual(len({unit["handle"] for unit in seen}), len(seen))

    def test_a_later_batch_sees_the_pages_earlier_batches_prepared(self):
        self.write("a.md", long_document(paragraphs=30))
        observed = []

        def model(payload, purpose, pages):
            observed.append([entry["slug"] for entry in pages])
            return answering_model([], slug="shared-topic")(payload, purpose, pages)

        with mock.patch.object(wiki, "material_budget", return_value=20_000):
            with mock.patch.object(wiki, "call_model", side_effect=model):
                wiki.do_update(self.root, args())

        self.assertEqual(observed[0], [])
        self.assertIn("shared-topic", observed[-1], "the model must see what it already drafted")

    def test_an_empty_result_still_completes_the_source(self):
        self.write("notes.md", "Nothing durable here.\n")
        with mock.patch.object(wiki, "call_model", return_value={"pages": [], "note": "nothing durable"}):
            wiki.do_update(self.root, args())
        state = wiki.load_state(self.root)
        self.assertEqual(state["files"]["notes.md"]["status"], "complete")
        self.assertEqual(list(self.pages_dir.glob("*.md")), [])
        self.assertEqual(wiki.lint(self.root), [])


class IdleRunTests(WikiIngestTestCase):
    def test_an_unchanged_workspace_calls_no_model(self):
        self.write("notes.md", "A decision.\n")
        with mock.patch.object(wiki, "call_model", side_effect=answering_model([])):
            wiki.do_update(self.root, args())
        with mock.patch.object(wiki, "call_model", side_effect=AssertionError("no model call")):
            output = StringIO()
            with redirect_stdout(output):
                wiki.do_update(self.root, args())
        self.assertIn("already current", output.getvalue())

    def test_status_and_scan_report_the_open_run(self):
        self.write("notes.md", long_document(paragraphs=30))
        state, records = self.rebuild()
        wiki.create_run(self.root, wiki.plan_ingest(state, records))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(wiki.main(["status", "--root", str(self.root)]), 0)
        rendered = output.getvalue()
        self.assertIn("notes.md", rendered)
        self.assertIn("deferred", rendered)


class EpisodeDeliveryTests(WikiIngestTestCase):
    """An episode must reach the model, or not be recorded as processed.

    A run with no pending chunks still has to deliver pending episodes. Skipping
    that call records the episode as processed without sending it anywhere, which
    loses the knowledge silently and permanently.
    """

    def test_an_episode_alone_is_delivered(self):
        calls = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(calls)) as model:
            wiki.do_update(self.root, args(episode="The team chose SQLite."))

        self.assertTrue(calls, "the episode must reach the model")
        self.assertEqual(len(model.call_args_list), 1)
        state = wiki.load_state(self.root)
        self.assertEqual(len(state["processed_episodes"]), 1, "only delivered episodes are recorded")

    def test_an_episode_after_every_source_is_complete_is_still_delivered(self):
        self.write("notes.md", "A decision.\n")
        first = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(first)):
            wiki.do_update(self.root, args())
        self.assertEqual(len(first), 1)

        second = []
        with mock.patch.object(wiki, "call_model", side_effect=answering_model(second)) as model:
            wiki.do_update(self.root, args(episode="A later decision."))

        self.assertEqual(len(model.call_args_list), 1, "the second episode must be delivered")
        self.assertTrue(second, "the episode must reach the model")
        episodes_in_payload = [
            item for payload in second for item in payload.get("episodes", [])
        ]
        self.assertTrue(episodes_in_payload, "the payload must carry the pending episode")

    def test_an_episode_is_not_recorded_when_the_model_call_fails(self):
        with mock.patch.object(wiki, "call_model", side_effect=wiki.WikiError("model unavailable")):
            with self.assertRaises(wiki.WikiError):
                wiki.do_update(self.root, args(episode="Never delivered."))

        state = wiki.load_state(self.root)
        self.assertEqual(state["processed_episodes"], [], "an undelivered episode stays pending")
        self.assertEqual(len(wiki.pending_episodes(self.root, state)), 1)


class ResumeEvidenceTests(WikiIngestTestCase):
    """A retry must reuse the knowledge the first attempt already extracted.

    Marking a chunk done only means "do not ask the model again". The pages that
    chunk produced still have to reach the final commit, or the retry silently
    drops everything the surviving chunks taught it.
    """

    def setUp(self):
        super().setUp()
        for number in range(1, 4):
            self.write(f"doc-{number}.md", f"Fact {number}: delivery date 2026-09-0{number}.\n")

    def accumulating_model(self, calls):
        def model(payload, purpose, pages):
            calls.append(payload)
            sources = set()
            for entry in pages:
                sources.update(entry.get("sources", []))
            sources.update(unit["source_id"] for unit in payload["chunks"])
            return {
                "pages": [{
                    "slug": "delivery-plan",
                    "title": "Delivery plan",
                    "type": "decision",
                    "status": "current",
                    "tags": ["delivery"],
                    "summary": "Delivery dates.",
                    "body": "Consolidated delivery dates.",
                    "sources": sorted(sources),
                }],
                "note": "recorded",
            }

        return model

    def one_file_per_batch(self):
        return ONE_FILE_BATCH_CHARS

    def test_evidence_from_before_the_failure_reaches_the_final_page(self):
        first = []
        with mock.patch.object(wiki, "material_budget", return_value=self.one_file_per_batch()):
            with mock.patch.object(wiki, "call_model", side_effect=self.accumulating_model(first)):
                wiki.do_update(self.root, args())
        # Everything succeeded, so this is the all-in-one-page baseline.
        baseline = (self.pages_dir / "delivery-plan.md").read_text(encoding="utf-8")
        for number in range(1, 4):
            self.assertIn(f"file:doc-{number}.md@sha256:", baseline)
        (self.pages_dir / "delivery-plan.md").unlink()
        state = wiki.load_state(self.root)
        state["files"] = {}
        state["pages"] = {}
        wiki.write_json(self.root / ".llm-wiki" / "state.json", state)

        calls = []

        def flaky(payload, purpose, pages):
            calls.append(payload)
            if len(calls) == 2:
                raise wiki.WikiError("model unavailable")
            return self.accumulating_model([])(payload, purpose, pages)

        with mock.patch.object(wiki, "material_budget", return_value=self.one_file_per_batch()):
            with mock.patch.object(wiki, "call_model", side_effect=flaky):
                with self.assertRaises(wiki.WikiError):
                    wiki.do_update(self.root, args())
        self.assertGreaterEqual(len(calls), 2, "at least two batches ran before the failure")

        retry = []
        with mock.patch.object(wiki, "material_budget", return_value=self.one_file_per_batch()):
            with mock.patch.object(wiki, "call_model", side_effect=self.accumulating_model(retry)):
                wiki.do_update(self.root, args())

        final = (self.pages_dir / "delivery-plan.md").read_text(encoding="utf-8")
        # The front matter records the update time at second resolution, so two runs
        # a second apart differ there by design. Comparing it would test the clock
        # rather than what this case is about, which is whether the evidence from the
        # batches that finished before the failure reached the final page.
        self.assertEqual(
            knowledge_of(final),
            knowledge_of(baseline),
            "the retry produces the same page knowledge as one clean run",
        )
        for text in (baseline, final):
            self.assertTrue(
                any(
                    line.startswith('updated_at: "') and "T" in line
                    for line in text.splitlines()
                ),
                "the page still records when it was written",
            )


if __name__ == "__main__":
    unittest.main()
