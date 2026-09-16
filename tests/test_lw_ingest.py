import importlib.util
import json
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
        self.assertEqual(final, baseline, "the retry produces the same page as one clean run")


if __name__ == "__main__":
    unittest.main()
