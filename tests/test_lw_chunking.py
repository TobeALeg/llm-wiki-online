import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
SCRIPTS = REPO_ROOT / "skills" / "lw" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import chunking  # noqa: E402


def joined(chunks):
    return "\n".join(chunk.text for chunk in chunks)


def compact(text):
    """Drop every whitespace character; keeps order, ignores line wrapping."""

    return "".join(text.split())


def non_blank_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


class ChunkAssertions(unittest.TestCase):
    """Shared invariants every chunk list must satisfy, whatever the input."""

    def assert_invariants(self, original, chunks, max_chars):
        self.assertIsInstance(chunks, list)
        for position, chunk in enumerate(chunks, start=1):
            self.assertEqual(chunk.index, position, "chunks are numbered in emission order")
            self.assertEqual(chunk.chunk_id, f"chunk-{position:03d}")
            self.assertLessEqual(len(chunk.text), max_chars, f"{chunk.chunk_id} respects the hard cap")
            self.assertGreater(len(chunk.text), 0, f"{chunk.chunk_id} is not empty")
            self.assertGreaterEqual(chunk.start, 0)
            self.assertLess(chunk.start, chunk.end, f"{chunk.chunk_id} has a forward range")
            self.assertLessEqual(chunk.end, len(original), f"{chunk.chunk_id} stays inside the source")
            self.assertIsInstance(chunk.heading_path, tuple)
            for heading in chunk.heading_path:
                self.assertIsInstance(heading, str)
            if chunk.verbatim:
                self.assertEqual(
                    chunk.text,
                    original[chunk.start:chunk.end],
                    f"{chunk.chunk_id} claims verbatim but its text differs from its range",
                )
        for earlier, later in zip(chunks, chunks[1:]):
            self.assertLessEqual(
                earlier.end, later.start, f"{earlier.chunk_id} overlaps {later.chunk_id}"
            )

    def assert_no_content_lost(self, original, chunks):
        """Every non-blank source line survives into the material sent to the model.

        Compared with whitespace removed, because a chunk boundary may legitimately
        fall inside a long line and rejoin it with a newline.
        """

        haystack = compact(joined(chunks))
        missing = [line for line in non_blank_lines(original) if compact(line) not in haystack]
        self.assertEqual(missing, [], "these source lines never reached any chunk")

    def assert_covers_source(self, original, chunks):
        """Nothing between the first and last character is dropped."""

        self.assertEqual("".join(chunk.text for chunk in chunks), original)


class EmptyAndShortTests(ChunkAssertions):
    def test_empty_and_whitespace_only_sources_produce_no_chunks(self):
        for text in ("", "   ", "\n\n", " \t\n   \n"):
            self.assertEqual(chunking.chunk_text(text), [], f"input {text!r}")

    def test_short_source_is_one_whole_chunk(self):
        text = "Point one.\n\nPoint two.\n"
        chunks = chunking.chunk_text(text)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.chunk_id, "chunk-001")
        self.assertTrue(chunk.verbatim)
        self.assertEqual(chunk.text.strip(), text.strip())
        self.assertEqual(chunk.text, text[chunk.start:chunk.end])

    def test_a_long_unbroken_source_is_sent_in_full_across_chunks(self):
        """The old 24,000 character excerpt is gone, and nothing is lost in its place."""

        text = "x" * 30_000
        chunks = chunking.chunk_text(text)
        self.assertGreater(len(chunks), 1)
        self.assert_covers_source(text, chunks)
        self.assert_invariants(text, chunks, chunking.MAX_CHUNK_CHARS)


class HeadingTests(ChunkAssertions):
    def test_heading_path_is_recorded_without_entering_the_offsets(self):
        text = "# Top\n\nAlpha body.\n\n## Sub\n\nBeta body.\n"
        chunks = chunking.chunk_text(text)
        self.assert_no_content_lost(text, chunks)
        alpha = next(chunk for chunk in chunks if "Alpha body." in chunk.text)
        beta = next(chunk for chunk in chunks if "Beta body." in chunk.text)
        self.assertEqual(alpha.heading_path, ("Top",))
        self.assertEqual(beta.heading_path, ("Top", "Sub"))
        self.assertEqual(chunks[0].text, text[chunks[0].start:chunks[0].end])

    def test_a_heading_starts_a_new_chunk(self):
        filler = "filler line\n" * 50
        text = f"# First\n\n{filler}\n# Second\n\n{filler}"
        chunks = chunking.chunk_text(text, target_chars=200, max_chars=400)
        paths = [chunk.heading_path for chunk in chunks]
        self.assertIn(("First",), paths)
        self.assertIn(("Second",), paths)
        second = next(chunk for chunk in chunks if chunk.text.startswith("# Second"))
        self.assertEqual(second.heading_path, ("Second",))
        self.assertNotIn("filler line", second.text.splitlines()[0])

    def test_a_narrower_budget_produces_more_chunks(self):
        text = "para one.\n\npara two.\n\npara three.\n"
        wide = chunking.chunk_text(text, target_chars=400, max_chars=400)
        narrow = chunking.chunk_text(text, target_chars=10, max_chars=12)
        self.assertGreater(len(narrow), len(wide))


class CodeFenceTests(ChunkAssertions):
    def test_heading_like_lines_inside_a_fence_are_not_headings(self):
        text = (
            "# Real heading\n\n"
            "```python\n"
            "# not a heading\n"
            "print('hi')\n"
            "```\n\n"
            "After the fence.\n"
        )
        chunks = chunking.chunk_text(text)
        after = next(chunk for chunk in chunks if "After the fence." in chunk.text)
        self.assertEqual(after.heading_path, ("Real heading",))
        self.assertTrue(all("not a heading" not in heading for chunk in chunks for heading in chunk.heading_path))

    def test_oversized_fence_is_split_with_the_fence_reopened(self):
        body = "\n".join(f"line {number} of code" for number in range(200))
        text = f"```python\n{body}\n```\n"
        chunks = chunking.chunk_text(text, target_chars=300, max_chars=400)
        self.assertGreater(len(chunks), 1)
        self.assert_invariants(text, chunks, 400)
        self.assert_no_content_lost(text, chunks)
        for chunk in chunks:
            self.assertTrue(chunk.text.lstrip().startswith("```"), "each piece reopens the fence")
            self.assertTrue(chunk.text.rstrip().endswith("```"), "each piece closes the fence")
            self.assertEqual(chunk.heading_path, ())

    def test_a_fence_without_a_closing_line_is_still_bounded(self):
        text = "```\n" + "\n".join(f"row {number}" for number in range(100))
        chunks = chunking.chunk_text(text, target_chars=200, max_chars=300)
        self.assert_invariants(text, chunks, 300)
        self.assert_no_content_lost(text, chunks)


class TableTests(ChunkAssertions):
    def test_table_pieces_repeat_the_header(self):
        rows = [f"| row {number} | value {number} |" for number in range(200)]
        text = "| name | value |\n| --- | --- |\n" + "\n".join(rows) + "\n"
        chunks = chunking.chunk_text(text, target_chars=300, max_chars=400)
        self.assertGreater(len(chunks), 1)
        self.assert_invariants(text, chunks, 400)
        self.assert_no_content_lost(text, chunks)
        for chunk in chunks:
            self.assertIn("| name | value |", chunk.text, "the header travels with every piece")
            self.assertIn("| --- | --- |", chunk.text)

    def test_a_small_table_is_one_verbatim_chunk(self):
        text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        chunks = chunking.chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].verbatim)


class BudgetTests(ChunkAssertions):
    def long_source(self, paragraphs=40, width=1_000):
        blocks = [
            f"Paragraph {number}: " + f"word{number} " * (width // 8) for number in range(paragraphs)
        ]
        blocks.append("Tail: the delivery date is 2026-09-30.")
        return "\n\n".join(blocks) + "\n"

    def test_a_thirty_thousand_character_source_keeps_its_tail(self):
        """The P0 regression: nothing past 24,000 characters may be dropped."""

        text = self.long_source()
        self.assertGreater(len(text), 24_000)
        chunks = chunking.chunk_text(text)
        self.assert_invariants(text, chunks, chunking.MAX_CHUNK_CHARS)
        self.assert_no_content_lost(text, chunks)
        self.assertIn("the delivery date is 2026-09-30", chunks[-1].text)

    def test_every_chunk_respects_the_hard_cap_on_adversarial_input(self):
        text = "\n\n".join(
            [
                "plain paragraph " * 400,
                "```sh\n" + ("echo hello\n" * 900) + "```",
                "| h1 | h2 |\n| --- | --- |\n" + ("| a | b |\n" * 900),
                "y" * 40_000,
                "# heading after the giants\n\nsmall tail\n",
            ]
        )
        chunks = chunking.chunk_text(text, target_chars=500, max_chars=700)
        self.assert_invariants(text, chunks, 700)
        self.assertIn("small tail", joined(chunks))

    def test_an_unsplittable_line_is_cut_on_a_boundary_and_still_capped(self):
        text = "z" * 50_000
        chunks = chunking.chunk_text(text, target_chars=1_000, max_chars=1_200)
        self.assert_invariants(text, chunks, 1_200)
        self.assert_covers_source(text, chunks)

    def test_an_oversized_heading_line_is_still_capped(self):
        """A heading longer than the cap must not become an oversized chunk."""

        text = "# " + "T" * 5_000 + "\n\nbody after the heading\n"
        chunks = chunking.chunk_text(text, target_chars=200, max_chars=300)
        self.assert_invariants(text, chunks, 300)
        self.assert_no_content_lost(text, chunks)

    def test_output_is_deterministic(self):
        text = self.long_source(paragraphs=12)
        first = chunking.chunk_text(text, target_chars=800, max_chars=1_000)
        second = chunking.chunk_text(text, target_chars=800, max_chars=1_000)
        self.assertEqual(first, second)
        self.assertEqual(
            chunking.parse_id("sha256:abc", first, target_chars=800, max_chars=1_000),
            chunking.parse_id("sha256:abc", second, target_chars=800, max_chars=1_000),
        )


class ParseIdTests(unittest.TestCase):
    def setUp(self):
        self.text = "# Doc\n\nAlpha facts.\n\nBeta facts.\n"

    def identity(self, text=None, revision="sha256:rev1", target=400, max_chars=500):
        chunks = chunking.chunk_text(text or self.text, target_chars=target, max_chars=max_chars)
        return chunking.parse_id(revision, chunks, target_chars=target, max_chars=max_chars)

    def test_parse_id_is_stable_for_the_same_input(self):
        self.assertEqual(self.identity(), self.identity())

    def test_parse_id_tracks_the_revision(self):
        self.assertNotEqual(self.identity(), self.identity(revision="sha256:rev2"))

    def test_parse_id_tracks_the_content(self):
        self.assertNotEqual(self.identity(), self.identity(text="# Doc\n\nGamma facts.\n"))

    def test_parse_id_tracks_the_parse_options(self):
        """Changing the parser configuration must not silently reuse old chunk offsets."""

        self.assertNotEqual(self.identity(), self.identity(target=100, max_chars=120))


class FuzzTests(ChunkAssertions):
    """Seeded random documents, so the invariants are checked on shapes nobody hand-wrote."""

    def random_document(self, random):
        pieces = []
        for _ in range(random.randint(1, 40)):
            kind = random.choice(["para", "heading", "fence", "table", "blank", "huge"])
            if kind == "para":
                pieces.append(random.choice(["alpha", "beta gamma", "字" * random.randint(1, 40)]))
            elif kind == "heading":
                pieces.append(
                    "#" * random.randint(1, 6) + " " + random.choice(["Title", "节", "x"])
                )
            elif kind == "fence":
                body = "\n".join(f"code {number}" for number in range(random.randint(0, 30)))
                pieces.append(f"```{random.choice(['py', ''])}\n{body}\n```")
            elif kind == "table":
                rows = "\n".join(
                    f"| {index} | {index * 2} |" for index in range(random.randint(1, 30))
                )
                pieces.append(f"| a | b |\n| --- | --- |\n{rows}")
            elif kind == "huge":
                pieces.append(random.choice(["z", "w "]) * random.randint(1, 900))
            else:
                pieces.append("")
        return "\n\n".join(pieces) + random.choice(["", "\n", "\n\n"])

    def test_invariants_hold_across_seeded_documents(self):
        import random

        generator = random.Random(20260916)
        for case in range(60):
            text = self.random_document(generator)
            target = generator.choice([40, 120, 400, 1_000])
            limit = generator.choice([target, target + 50, 4_000])
            with self.subTest(case=case, length=len(text), target=target, limit=limit):
                chunks = chunking.chunk_text(text, target_chars=target, max_chars=limit)
                if not text.strip():
                    self.assertEqual(chunks, [])
                    continue
                self.assert_invariants(text, chunks, limit)
                self.assert_no_content_lost(text, chunks)


if __name__ == "__main__":
    unittest.main()
