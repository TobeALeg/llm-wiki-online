"""One permanent address space, one width.

An address that a citation already points at cannot change width without
invalidating the citation, so the widths were frozen the day they were chosen. The
danger is drift: a minting function narrowing its digest, or a validation pattern
widening to match, until the two silently disagree about what an address is. This
file pins both halves against what the minting functions actually produce.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import chunking, evidence, knowledge_pipeline  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore, new_id  # noqa: E402
from llm_wiki_mcp.knowledge_types import (  # noqa: E402
    ARTIFACT_ID_PATTERN,
    CLAIM_ID_PATTERN,
    CLAIM_VERSION_PATTERN,
    CONTENT_ID_HEX,
    EVIDENCE_ID_PATTERN,
    ORIGIN_ID_PATTERN,
    PAGE_ID_PATTERN,
    RELATION_ID_PATTERN,
    REVISION_ID_PATTERN,
    SUPPORT_GROUP_PATTERN,
    TOPIC_ID_PATTERN,
)

CONTENT_ADDRESSES = {
    "artifact": ("art_", ARTIFACT_ID_PATTERN),
    "evidence": ("evd_", EVIDENCE_ID_PATTERN),
}

STORE_ADDRESSES = {
    "claim": ("clm_", CLAIM_ID_PATTERN),
    "claim_version": ("clv_", CLAIM_VERSION_PATTERN),
    "origin": ("org_", ORIGIN_ID_PATTERN),
    "topic": ("top_", TOPIC_ID_PATTERN),
    "revision": ("rev_", REVISION_ID_PATTERN),
    "support_group": ("sgr_", SUPPORT_GROUP_PATTERN),
    "relation": ("rel_", RELATION_ID_PATTERN),
    "page": ("pag_", PAGE_ID_PATTERN),
}


class ContentAddressWidthTests(unittest.TestCase):
    def setUp(self):
        self.text = "# Title\n\n仅在低数据量场景使用 SQLite。\n"
        self.artifact = evidence.freeze_artifact(
            revision_id="rev_" + "a" * 32,
            text=self.text,
            parser_name=chunking.PARSER_NAME,
            parser_version=chunking.PARSER_VERSION,
            config_hash="width",
            structure=chunking.artifact_structure(self.text),
        )

    def test_a_frozen_artifact_is_addressed_by_a_whole_digest(self):
        self.assertEqual(len(self.artifact.artifact_id), len("art_") + 64)
        self.assertTrue(ARTIFACT_ID_PATTERN.fullmatch(self.artifact.artifact_id))

    def test_a_citation_is_addressed_by_a_whole_digest(self):
        record = evidence.make_evidence(
            project_id="width", artifact=self.artifact, spans=[(0, 3)]
        )
        self.assertEqual(len(record.evidence_id), len("evd_") + 64)
        self.assertTrue(EVIDENCE_ID_PATTERN.fullmatch(record.evidence_id))

    def test_a_truncated_content_address_is_refused(self):
        for pattern, prefix in (
            (ARTIFACT_ID_PATTERN, "art_"),
            (EVIDENCE_ID_PATTERN, "evd_"),
        ):
            with self.subTest(prefix=prefix):
                self.assertIsNone(
                    pattern.fullmatch(prefix + "a" * 32),
                    "a shortened content address must not validate",
                )
                self.assertIsNone(pattern.fullmatch(prefix + "a" * 63))
                self.assertIsNone(pattern.fullmatch(prefix + "a" * 65))

    def test_the_pipeline_does_not_narrow_a_content_address(self):
        """A pipeline that truncated a digest would make a collision reachable."""

        self.assertEqual(CONTENT_ID_HEX, r"[0-9a-f]{64}")
        self.assertFalse(
            hasattr(knowledge_pipeline, "contract_evidence_id"),
            "the id-narrowing detour must stay deleted",
        )
        self.assertFalse(hasattr(knowledge_pipeline, "evidence_alias"))


class StoreAddressWidthTests(unittest.TestCase):
    def test_a_store_minted_id_is_32_hex_for_every_prefix(self):
        for name, (prefix, pattern) in STORE_ADDRESSES.items():
            with self.subTest(address=name):
                minted = new_id(prefix)
                self.assertEqual(len(minted), len(prefix) + 32)
                self.assertTrue(
                    pattern.fullmatch(minted),
                    f"{name} ids must satisfy their own pattern: {minted}",
                )

    def test_a_content_width_id_is_refused_where_a_store_id_is_expected(self):
        for name, (prefix, pattern) in STORE_ADDRESSES.items():
            with self.subTest(address=name):
                self.assertIsNone(
                    pattern.fullmatch(prefix + "a" * 64),
                    "a store-assigned address space is 32 hex, not 64",
                )

    def test_the_store_mints_one_width_in_practice(self):
        """Read real ids back from a real store rather than only from `new_id`."""

        directory = tempfile.mkdtemp()
        store = ClaimStore(Path(directory) / "knowledge.sqlite3")
        from llm_wiki_mcp.knowledge_types import Scope

        scope = Scope.of("local", "width")
        revision = store.freeze_revision(
            scope=scope,
            source_id="file:width.md",
            source_type="file",
            label="width.md",
            raw_content="# Width\n\nBody.\n",
        )
        topic_id = store.ensure_topic(scope=scope, canonical_label="Width")
        for name, value, pattern in (
            ("revision", revision["revision_id"], REVISION_ID_PATTERN),
            ("topic", topic_id, TOPIC_ID_PATTERN),
        ):
            with self.subTest(address=name):
                self.assertTrue(pattern.fullmatch(value), f"{name} was {value}")


class PatternShapeTests(unittest.TestCase):
    def test_each_prefix_appears_in_exactly_one_address_space(self):
        content_prefixes = {prefix for prefix, _ in CONTENT_ADDRESSES.values()}
        store_prefixes = {prefix for prefix, _ in STORE_ADDRESSES.values()}
        self.assertEqual(set(), content_prefixes & store_prefixes)
        self.assertEqual({"art_", "evd_"}, content_prefixes)

    def test_the_two_width_constants_are_the_only_shape_sources(self):
        """Every pattern is built from one of two constants, not a local literal."""

        source = (REPO_ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp" / "knowledge_types.py").read_text(
            encoding="utf-8"
        )
        literals = re.findall(r"re\.compile\(rf\^[a-z]+_\\\\", source)
        self.assertEqual([], literals, "a pattern rebuilt a width literal inline")

    def test_the_web_routes_use_the_shared_widths(self):
        source = (REPO_ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp" / "webapp.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("STORE_ID_HEX", source)
        self.assertIn("CONTENT_ID_HEX", source)
        self.assertNotIn("[0-9a-f]{32}(?:[0-9a-f]{32})?", source, "no second width definition")


if __name__ == "__main__":
    unittest.main()
