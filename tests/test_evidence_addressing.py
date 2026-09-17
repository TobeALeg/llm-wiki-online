"""E01 to E08: a citation recovers the exact text it names, or fails loudly.

Every case runs the path the ingest actually runs: freeze a revision, freeze the
parser's artifact beside it, chunk it, cite a chunk, and read the citation back
through a real ClaimStore. The expected values are literal, because the point of
an address is that its arithmetic can be checked by hand.
"""

import ast
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).parent))

from acceptance import case  # noqa: E402
from llm_wiki_mcp import chunking, evidence  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore  # noqa: E402
from llm_wiki_mcp.knowledge_types import Scope  # noqa: E402

PROJECT_ID = "evidence-tests"
PARSER_NAME = "structural"
STDLIB_IMPORTS = {"__future__", "hashlib", "json", "dataclasses", "typing"}
VENDORED_SIBLINGS = {"knowledge_types", "chunking", "evidence"}
"""Modules vendored beside this one. They ship together, so importing one is not a
dependency on anything a clean skill install lacks. `knowledge_types` is the root
of that graph and imports no sibling, which the parity test asserts."""

CHINESE = "中文"
EMOJI = "\U0001F600"  # grinning face, one code point
FAMILY = "\U0001F468\u200D\U0001F469\u200D\U0001F467"  # man + ZWJ + woman + ZWJ + girl
COMBINING = "e\u0301"  # e followed by a combining acute accent
CROSSING = CHINESE + " notes\nplain ascii row"  # straddles the \r\n of the raw document


def imported_roots(path):
    """Every top level module named by an import statement in a file."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class EvidenceFixture(unittest.TestCase):
    """A real store under a temp directory, and the freeze path ingest uses."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "knowledge.sqlite3"
        self.store = ClaimStore(self.database)
        self.scope = Scope.of("local", PROJECT_ID)

    def freeze(
        self,
        text,
        *,
        source_id,
        label="Frozen note",
        parser_name=PARSER_NAME,
        parser_version="1",
        config_hash="cfg-1",
        raw_available=True,
        target_chars=chunking.TARGET_CHUNK_CHARS,
        max_chars=chunking.MAX_CHUNK_CHARS,
    ):
        """Freeze one document the way ingest does: revision, artifact, chunks."""

        revision = self.store.freeze_revision(
            scope=self.scope,
            source_id=source_id,
            source_type="note",
            label=label,
            raw_content=text,
            raw_available=raw_available,
        )
        normalized = evidence.normalize_text(text)
        artifact = evidence.freeze_artifact(
            revision_id=revision["revision_id"],
            text=normalized,
            parser_name=parser_name,
            parser_version=parser_version,
            config_hash=config_hash,
            structure=chunking.artifact_structure(normalized),
        )
        chunks = chunking.chunk_text(normalized, target_chars=target_chars, max_chars=max_chars)
        self.store.store_artifact(scope=self.scope, artifact=artifact, chunks=chunks)
        return revision, artifact, chunks

    def cite(self, artifact, spans, *, context_refs=(), label=""):
        record = evidence.make_evidence(
            project_id=self.scope.project_id,
            artifact=artifact,
            spans=spans,
            structural_context_refs=context_refs,
            label=label,
        )
        self.store.register_evidence(record, scope=self.scope)
        return record

    def recovered(self, record):
        return self.store.load_evidence(record.evidence_id, self.scope)

    def query(self, statement, parameters=()):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()

    def sql(self, statement, parameters=()):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()


class AddressStabilityTests(EvidenceFixture):
    @case("E01")
    def test_reprocessing_the_same_revision_and_config_mints_one_artifact(self):
        text = (
            "# Stability\n\n"
            "Latency stayed under 200 ms all week.\n\n"
            "Throughput held at 40 requests per second.\n\n"
            "No incident was opened against the cluster.\n"
        )
        first_revision, first_artifact, first_chunks = self.freeze(
            text, source_id="src-stable", target_chars=80, max_chars=120
        )
        second_revision, second_artifact, second_chunks = self.freeze(
            text, source_id="src-stable", target_chars=80, max_chars=120
        )

        self.assertGreater(len(first_chunks), 1, "the fixture needs more than one chunk to compare")
        self.assertEqual(second_revision["revision_id"], first_revision["revision_id"])
        self.assertEqual(second_artifact.artifact_id, first_artifact.artifact_id)
        self.assertEqual(
            [
                (chunk.chunk_id, chunk.start, chunk.end, chunk.evidence_spans, chunk.render_recipe)
                for chunk in second_chunks
            ],
            [
                (chunk.chunk_id, chunk.start, chunk.end, chunk.evidence_spans, chunk.render_recipe)
                for chunk in first_chunks
            ],
        )
        self.assertEqual(second_artifact.normalized_sha256, first_artifact.normalized_sha256)

        spans = first_chunks[0].evidence()
        first_evidence = self.cite(first_artifact, spans)
        second_evidence = self.cite(second_artifact, spans)
        self.assertEqual(second_evidence.evidence_id, first_evidence.evidence_id)
        self.assertEqual(second_evidence.span_hashes, first_evidence.span_hashes)

        first = self.recovered(first_evidence)
        second = self.recovered(second_evidence)
        self.assertEqual(first.exact_text, second.exact_text)
        self.assertEqual(first.exact_text.encode("utf-8"), second.exact_text.encode("utf-8"))
        self.assertEqual(first.exact_text, text[spans[0][0] : spans[0][1]])

        _, reconfigured, _ = self.freeze(
            text, source_id="src-stable", target_chars=80, max_chars=120, config_hash="cfg-2"
        )
        self.assertNotEqual(reconfigured.artifact_id, first_artifact.artifact_id)
        moved = self.cite(reconfigured, spans)
        self.assertNotEqual(moved.evidence_id, first_evidence.evidence_id)

        still = self.recovered(first_evidence)
        self.assertEqual(still.artifact_id, first_artifact.artifact_id)
        self.assertEqual(still.exact_text, first.exact_text)


class CodePointOffsetTests(EvidenceFixture):
    def unicode_document(self):
        raw = (
            "# 中文 notes\r\n"
            "plain ascii row\r\n"
            "\U0001F600 single code point\r\n"
            "\U0001F468\u200D\U0001F469\u200D\U0001F467 family sequence\r\n"
            "cafe\u0301 combining\r\n"
            "ascii 中文 tail\r\n"
        )
        return raw, evidence.normalize_text(raw)

    @case("E02")
    def test_offsets_count_unicode_code_points_not_bytes_or_code_units(self):
        raw, text = self.unicode_document()
        self.assertEqual(len(CHINESE), 2)
        self.assertEqual(len(EMOJI), 1)
        self.assertEqual(len(FAMILY), 5)
        self.assertEqual(len(COMBINING), 2)
        self.assertEqual(raw.count("\r\n"), 6)
        self.assertNotIn("\r", text)
        self.assertIn(CROSSING.replace("\n", "\r\n"), raw)

        _, artifact, _ = self.freeze(raw, source_id="src-unicode")

        for token in (CHINESE, EMOJI, FAMILY, COMBINING, CROSSING):
            start = text.index(token)
            span = (start, start + len(token))
            record = self.cite(artifact, (span,))
            recovered = self.recovered(record)
            self.assertEqual(recovered.exact_text, token, f"{token!r} at {span}")
            self.assertEqual(record.offset_unit, "unicode_code_point")

        self.assertEqual((text.index(CHINESE), text.index(CHINESE) + len(CHINESE)), (2, 4))
        self.assertEqual((text.index(EMOJI), text.index(EMOJI) + len(EMOJI)), (27, 28))
        self.assertEqual(
            [row[0] for row in self.query("SELECT DISTINCT offset_unit FROM evidence_refs")],
            ["unicode_code_point"],
        )

        chinese_start = text.index(CHINESE)
        self.assertNotEqual(len(CHINESE.encode("utf-8")), len(CHINESE))
        byte_span = (chinese_start, chinese_start + len(CHINESE.encode("utf-8")))
        self.assertEqual(byte_span, (2, 8))
        from_bytes = evidence.recover(
            record=evidence.make_evidence(
                project_id=self.scope.project_id, artifact=artifact, spans=(byte_span,)
            ),
            artifact=artifact,
            expected_project_id=self.scope.project_id,
        )
        self.assertEqual(from_bytes.exact_text, "中文 not")
        self.assertNotEqual(from_bytes.exact_text, CHINESE)

        emoji_start = text.index(EMOJI)
        utf16_span = (emoji_start, emoji_start + len(EMOJI.encode("utf-16-le")) // 2)
        self.assertEqual(utf16_span, (27, 29))
        from_utf16 = evidence.recover(
            record=evidence.make_evidence(
                project_id=self.scope.project_id, artifact=artifact, spans=(utf16_span,)
            ),
            artifact=artifact,
            expected_project_id=self.scope.project_id,
        )
        self.assertEqual(from_utf16.exact_text, EMOJI + " ")
        self.assertNotEqual(from_utf16.exact_text, EMOJI)

        solo = evidence.normalize_text(CHINESE + "\r\n")
        self.assertEqual(solo, "中文\n")
        self.assertEqual(len(solo), 3)
        solo_artifact = evidence.freeze_artifact(
            revision_id="rev_solo",
            text=solo,
            parser_name=PARSER_NAME,
            parser_version="1",
            config_hash="cfg-1",
        )
        with self.assertRaises(evidence.EvidenceError) as raised:
            evidence.make_evidence(
                project_id=self.scope.project_id,
                artifact=solo_artifact,
                spans=((0, len(CHINESE.encode("utf-8"))),),
            )
        self.assertEqual(raised.exception.code, "INVALID_SPAN")
        exact = evidence.recover(
            record=evidence.make_evidence(
                project_id=self.scope.project_id, artifact=solo_artifact, spans=((0, 2),)
            ),
            artifact=solo_artifact,
            expected_project_id=self.scope.project_id,
        )
        self.assertEqual(exact.exact_text, CHINESE)


class TableContextTests(EvidenceFixture):
    def table_document(self):
        header = "| metric | p95 | unit |"
        separator = "| --- | --- | --- |"
        rows = [f"| probe-{number:02d} | {number * 3} | ms |" for number in range(24)]
        footnote = "[^u]: Every p95 figure is in milliseconds."
        text = (
            "# Latency report\n\n"
            + header
            + "\n"
            + separator
            + "\n"
            + "\n".join(rows)
            + "\n"
            + footnote
            + "\n"
        )
        return text, header, separator, rows, footnote

    @case("E03")
    def test_a_split_table_cites_rows_and_restores_header_and_units(self):
        text, header, separator, rows, footnote = self.table_document()
        header_span = (text.index(header), text.index(header) + len(header))
        separator_span = (text.index(separator), text.index(separator) + len(separator))
        self.assertEqual((header_span, separator_span), ((18, 41), (42, 61)))

        _, artifact, chunks = self.freeze(
            text, source_id="src-table", target_chars=90, max_chars=90
        )
        table_chunks = [chunk for chunk in chunks if chunk.render_recipe == "table_with_header"]
        others = [chunk for chunk in chunks if chunk.render_recipe != "table_with_header"]
        self.assertGreater(len(table_chunks), 1, "the table must be split to prove anything")
        self.assertEqual([chunk.verbatim for chunk in others], [True, True])

        for chunk in table_chunks:
            self.assertFalse(chunk.verbatim, "a rebuilt table must not claim to be original text")
            self.assertNotEqual(chunk.text, text[chunk.start : chunk.end])
            self.assertEqual(chunk.context_spans, (header_span, separator_span))
            self.assertNotIn(header_span, chunk.evidence_spans)
            self.assertNotIn(separator_span, chunk.evidence_spans)
            for start, end in chunk.evidence_spans:
                self.assertIn(text[start:end], rows, "evidence covers data rows only")

        self.assertEqual(
            [text[start:end] for chunk in table_chunks for start, end in chunk.evidence_spans],
            rows,
            "the pieces cite every data row once, never the header",
        )

        structure = chunking.artifact_structure(text)
        self.assertEqual(structure["blocks"][1]["kind"], "table")
        context_index = 1
        self.assertEqual(
            evidence.structural_context(artifact, (context_index,)),
            (header, separator, footnote),
        )

        piece = table_chunks[0]
        record = self.cite(artifact, piece.evidence_spans, context_refs=(context_index,))
        recovered = self.recovered(record)
        self.assertEqual(recovered.exact_text, text[piece.start : piece.end])
        self.assertNotIn(header, recovered.exact_text, "the header is context, not a quote")
        self.assertIn("| probe-", recovered.exact_text)
        self.assertEqual(recovered.structural_context, (header, separator, footnote))


class FenceReconstructionTests(EvidenceFixture):
    def code_lines(self):
        return "\n".join(f"print({number})  # step {number}" for number in range(40))

    @case("E04")
    def test_a_synthesized_fence_is_context_and_can_never_be_cited(self):
        body = self.code_lines()
        closed = "```python\n" + body + "\n```\n"
        opening_span = (0, len("```python"))
        closing_span = (closed.rindex("```"), closed.rindex("```") + 3)
        self.assertEqual(closed[closing_span[0] : closing_span[1]], "```")

        _, closed_artifact, closed_chunks = self.freeze(
            closed, source_id="src-code-closed", target_chars=150, max_chars=200
        )
        self.assertGreater(len(closed_chunks), 1)
        for chunk in closed_chunks:
            self.assertEqual(chunk.render_recipe, "code_with_fence")
            self.assertEqual(chunk.render_notes, ("fence_repeated",))
            self.assertEqual(chunk.context_spans, (opening_span, closing_span))
            self.assertNotIn(opening_span, chunk.evidence_spans)
            self.assertNotIn(closing_span, chunk.evidence_spans)
            self.assertFalse(chunk.verbatim)
            quoted = self.recovered(self.cite(closed_artifact, chunk.evidence_spans))
            self.assertEqual(quoted.exact_text, closed[chunk.start : chunk.end])
            self.assertNotIn("```", quoted.exact_text, "no fence line is ever evidence")
        self.assertTrue(closed_chunks[-1].text.endswith("```"), "the real closing fence is re-emitted")
        self.assertLess(
            closed_chunks[-1].end,
            closing_span[0],
            "a piece's own range stops at its last code line",
        )

        unterminated = "```python\n" + body + "\n"
        self.assertNotIn("```\n```", unterminated)
        _, open_artifact, open_chunks = self.freeze(
            unterminated, source_id="src-code-open", target_chars=150, max_chars=200
        )
        self.assertGreater(len(open_chunks), 1)
        for chunk in open_chunks:
            self.assertEqual(chunk.render_recipe, "code_with_fence")
            self.assertEqual(chunk.render_notes, ("fence_closed_synthesized",))
            self.assertEqual(chunk.context_spans, (opening_span,))
            self.assertNotIn(opening_span, chunk.evidence_spans)
            self.assertFalse(chunk.verbatim)
            quoted = self.recovered(self.cite(open_artifact, chunk.evidence_spans))
            self.assertEqual(quoted.exact_text, unterminated[chunk.start : chunk.end])
            self.assertNotIn("```", quoted.exact_text, "no fence line is ever evidence")
        self.assertTrue(
            open_chunks[-1].text.endswith("```python"),
            "the last piece carries a fence the source never had",
        )


class RevisionHistoryTests(EvidenceFixture):
    @case("E05")
    def test_new_content_and_a_new_parser_never_shadow_an_old_citation(self):
        first_text = "# Plan\n\nThe launch date is 2026-09-30.\n"
        first_revision, first_artifact, first_chunks = self.freeze(
            first_text, source_id="src-mutating", label="Launch plan", config_hash="cfg-1"
        )
        first_evidence = self.cite(first_artifact, first_chunks[0].evidence())
        original = self.recovered(first_evidence)

        second_text = "# Plan\n\nThe launch date is 2026-10-15, after the review.\n"
        second_revision, second_artifact, second_chunks = self.freeze(
            second_text,
            source_id="src-mutating",
            label="Launch plan",
            parser_version="2",
            config_hash="cfg-2",
        )
        second_evidence = self.cite(second_artifact, second_chunks[0].evidence())

        self.assertNotEqual(second_revision["revision_id"], first_revision["revision_id"])
        status = self.store.source_status("src-mutating", self.scope)
        self.assertTrue(status["found"])
        self.assertEqual([item["ordinal"] for item in status["revisions"]], [1, 2])
        self.assertEqual(
            [item["revision_id"] for item in status["revisions"]],
            [first_revision["revision_id"], second_revision["revision_id"]],
        )
        self.assertEqual(status["revisions"][0]["raw_sha256"], first_revision["raw_sha256"])
        self.assertNotEqual(status["revisions"][1]["raw_sha256"], first_revision["raw_sha256"])

        self.assertNotEqual(second_artifact.artifact_id, first_artifact.artifact_id)
        self.assertEqual(
            [
                row[0]
                for row in self.query(
                    "SELECT artifact_id FROM parsed_artifacts WHERE revision_id = ?",
                    (first_revision["revision_id"],),
                )
            ],
            [first_artifact.artifact_id],
        )
        self.assertEqual(
            [
                row[0]
                for row in self.query(
                    "SELECT artifact_id FROM parsed_artifacts WHERE revision_id = ?",
                    (second_revision["revision_id"],),
                )
            ],
            [second_artifact.artifact_id],
        )

        self.assertIn("2026-09-30", original.exact_text)
        self.assertEqual(original.exact_text, "# Plan\n\nThe launch date is 2026-09-30.")
        still = self.recovered(first_evidence)
        self.assertEqual(still.artifact_id, first_artifact.artifact_id)
        self.assertEqual(still.exact_text, original.exact_text)
        self.assertEqual(still.span_hashes, first_evidence.span_hashes)
        self.assertEqual(still.source_label, "Launch plan")
        moved = self.recovered(second_evidence)
        self.assertEqual(moved.artifact_id, second_artifact.artifact_id)
        self.assertEqual(moved.exact_text, "# Plan\n\nThe launch date is 2026-10-15, after the review.")
        self.assertNotEqual(moved.exact_text, still.exact_text)

        again = self.store.freeze_revision(
            scope=self.scope,
            source_id="src-mutating",
            source_type="note",
            label="Launch plan",
            raw_content=second_text,
        )
        self.assertTrue(again["reused"])
        self.assertEqual(again["revision_id"], second_revision["revision_id"])
        self.assertEqual(len(self.store.source_status("src-mutating", self.scope)["revisions"]), 2)


class TamperedEvidenceTests(EvidenceFixture):
    @case("E06")
    def test_tampering_and_out_of_range_spans_refuse_to_return_text(self):
        text = "# Ops\n\nThe nightly build runs at 02:00 UTC.\n"
        _, artifact, chunks = self.freeze(text, source_id="src-tampered")
        record = self.cite(artifact, chunks[0].evidence())
        self.assertEqual(
            self.recovered(record).exact_text, text[chunks[0].start : chunks[0].end]
        )

        self.sql(
            "UPDATE parsed_artifacts SET normalized_text = ? WHERE artifact_id = ?",
            (text.replace("02:00", "03:00"), artifact.artifact_id),
        )
        with self.assertRaises(evidence.EvidenceError) as raised:
            self.recovered(record)
        self.assertEqual(raised.exception.code, "HASH_MISMATCH")
        self.assertFalse(hasattr(raised.exception, "exact_text"), "a failed load returns no text")
        self.assertNotIn("02:00", str(raised.exception))
        self.assertNotIn("03:00", str(raised.exception))

        _, overrun_artifact, overrun_chunks = self.freeze(text, source_id="src-overrun")
        honest_span = overrun_chunks[0].evidence()[0]
        past_the_end = (honest_span[0], len(overrun_artifact.normalized_text) + 5)
        with self.assertRaises(evidence.EvidenceError) as raised:
            evidence.make_evidence(
                project_id=self.scope.project_id, artifact=overrun_artifact, spans=(past_the_end,)
            )
        self.assertEqual(raised.exception.code, "INVALID_SPAN")

        forged = evidence.EvidenceRecord(
            evidence_id="evd_forged",
            project_id=self.scope.project_id,
            artifact_id=overrun_artifact.artifact_id,
            spans=(past_the_end,),
            span_hashes=(evidence.span_hash(overrun_artifact.normalized_text, *honest_span),),
        )
        with self.assertRaises(evidence.EvidenceError) as raised:
            evidence.recover(
                record=forged,
                artifact=overrun_artifact,
                expected_project_id=self.scope.project_id,
            )
        self.assertEqual(raised.exception.code, "INVALID_SPAN")
        self.assertFalse(hasattr(raised.exception, "exact_text"), "a clipped quote is never returned")

        overrun_record = self.cite(overrun_artifact, (honest_span,))
        self.sql(
            "UPDATE evidence_refs SET spans_json = ? WHERE evidence_id = ?",
            ("[[%d, %d]]" % past_the_end, overrun_record.evidence_id),
        )
        with self.assertRaises(evidence.EvidenceError) as raised:
            self.recovered(overrun_record)
        self.assertEqual(raised.exception.code, "INVALID_SPAN")
        self.assertFalse(hasattr(raised.exception, "exact_text"))

        self.assertEqual(imported_roots(Path(evidence.__file__)) - STDLIB_IMPORTS - VENDORED_SIBLINGS, set())
        self.assertEqual(
            [
                name
                for name in dir(evidence)
                if not name.startswith("_")
                and any(token in name.lower() for token in ("model", "prompt", "client", "http"))
            ],
            [],
            "evidence.py must have no call path to a model",
        )


class ParserIndependenceTests(EvidenceFixture):
    @case("E07")
    def test_an_installed_parser_is_not_needed_to_read_an_old_artifact(self):
        text = "# Legacy\n\n```sh\nmake check\n```\n"
        _, artifact, chunks = self.freeze(
            text, source_id="src-legacy", parser_name="retired-parser", parser_version="0.4"
        )
        structure = chunking.artifact_structure(text)
        code_index = next(
            index for index, block in enumerate(structure["blocks"]) if block["kind"] == "code"
        )
        self.assertEqual(code_index, 1)
        record = self.cite(artifact, chunks[0].evidence(), context_refs=(code_index,))

        with mock.patch.object(
            chunking, "chunk_text", side_effect=AssertionError("a citation must not re-parse")
        ):
            recovered = self.recovered(record)
        self.assertEqual(recovered.exact_text, text[chunks[0].start : chunks[0].end])
        self.assertEqual(recovered.structural_context, ("```sh", "```"))

        self.sql("DELETE FROM parsed_artifacts WHERE artifact_id = ?", (artifact.artifact_id,))
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM evidence_refs WHERE evidence_id = ?", (record.evidence_id,)
            )[0][0],
            1,
            "the citation row survives, only the artifact is gone",
        )
        with self.assertRaises(evidence.EvidenceError) as raised:
            self.recovered(record)
        self.assertEqual(raised.exception.code, "EVIDENCE_NOT_FOUND")
        self.assertFalse(hasattr(raised.exception, "exact_text"))


class RawAvailabilityTests(EvidenceFixture):
    @case("E08")
    def test_normalized_text_is_citable_without_claiming_a_stored_file(self):
        text = "# Submitted\n\nOnly the normalized text reached the server.\n"
        revision, artifact, chunks = self.freeze(
            text, source_id="src-normalized-only", raw_available=False
        )
        self.assertFalse(revision["raw_available"])
        self.assertFalse(self.store.raw_available("src-normalized-only", self.scope))
        status = self.store.source_status("src-normalized-only", self.scope)
        self.assertTrue(status["found"])
        self.assertEqual(len(status["revisions"]), 1)
        self.assertFalse(status["revisions"][0]["raw_available"])
        self.assertEqual(
            self.query(
                "SELECT storage_key FROM source_revisions WHERE revision_id = ?",
                (revision["revision_id"],),
            ),
            [(None,)],
            "nothing claims to hold a file the server never received",
        )

        record = self.cite(artifact, chunks[0].evidence(), label=chunks[0].chunk_id)
        recovered = self.recovered(record)
        self.assertFalse(recovered.raw_available)
        self.assertEqual(recovered.exact_text, text[chunks[0].start : chunks[0].end])

        _, raw_artifact, raw_chunks = self.freeze(
            text, source_id="src-with-raw", raw_available=True
        )
        self.assertTrue(self.store.raw_available("src-with-raw", self.scope))
        raw_record = self.cite(raw_artifact, raw_chunks[0].evidence())
        self.assertTrue(self.recovered(raw_record).raw_available)


class ArtifactDigestTests(EvidenceFixture):
    """The artifact-level digest check, which the span hashes cannot cover.

    Tampering inside a cited span is caught by comparing that span's own hash. The
    digest over the whole artifact is what catches tampering outside every cited
    span, so a test that only ever edits cited text leaves it unverified.
    """

    @case("E06")
    def test_tampering_outside_the_cited_span_is_caught_by_the_artifact_digest(self):
        text = "# Ops\n\nThe nightly build runs at 02:00 UTC and reports to #build.\n"
        _, artifact, chunks = self.freeze(text, source_id="src-outside")
        self.assertTrue(chunks)
        # Cite only the first clause, so the rest of the artifact is outside the
        # citation and no span hash covers it.
        cited = (0, text.index("02:00"))
        record = self.cite(artifact, (cited,))

        uncited_offset = text.index("reports to")
        self.assertGreater(uncited_offset, cited[1])
        altered = text[:uncited_offset] + "is lost and does not reach" + text[uncited_offset + len("reports to"):]
        self.assertEqual(altered[cited[0] : cited[1]], text[cited[0] : cited[1]])
        self.sql(
            "UPDATE parsed_artifacts SET normalized_text = ? WHERE artifact_id = ?",
            (altered, artifact.artifact_id),
        )

        with self.assertRaises(evidence.EvidenceError) as raised:
            self.recovered(record)
        self.assertEqual(
            raised.exception.code,
            "HASH_MISMATCH",
            "an edit outside the cited span must be refused by the artifact digest",
        )
        self.assertFalse(hasattr(raised.exception, "exact_text"))

    @case("E06")
    def test_verify_artifact_alone_refuses_an_edited_snapshot(self):
        """Called directly, so the guard is proven even if a caller skips recover."""

        text = "# Ops\n\nBody.\n"
        _, artifact, _chunks = self.freeze(text, source_id="src-direct")
        evidence.verify_artifact(artifact)

        from dataclasses import replace as dataclass_replace

        tampered = dataclass_replace(
            artifact, normalized_text=artifact.normalized_text + " an added line"
        )
        with self.assertRaises(evidence.EvidenceError) as raised:
            evidence.verify_artifact(tampered)
        self.assertEqual(raised.exception.code, "HASH_MISMATCH")


class OffsetUnitParityTests(unittest.TestCase):
    def test_the_chunker_and_the_evidence_module_agree_on_the_offset_unit(self):
        self.assertEqual(chunking.OFFSET_UNIT, "unicode_code_point")
        self.assertEqual(chunking.OFFSET_UNIT, evidence.OFFSET_UNIT)


if __name__ == "__main__":
    unittest.main()
