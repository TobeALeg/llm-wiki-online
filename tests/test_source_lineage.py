"""A repost is the same witness, not a second one.

The spec forbids letting a copy raise a claim's standing: re-importing one article,
or a page that summarises a conversation already held, is the same material seen
twice. Counting it as independent corroboration is how a claim acquires a
confidence nobody earned, so the count distinguishes the two.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import chunking, evidence, knowledge_types as kt  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore  # noqa: E402

SAME_TEXT = "# 存储选型\n\n仅在当前低数据量场景使用 SQLite，暂不引入 Postgres。\n"
OTHER_TEXT = "# 复盘\n\n当天的构建在 02:00 UTC 失败，原因是缓存目录写满。\n"


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ClaimStore(Path(self.temporary.name) / "knowledge.sqlite3")
        self.scope = kt.Scope.of("local", "lineage")

    def tearDown(self):
        self.temporary.cleanup()

    def _cite(self, text, source_id, needle="SQLite"):
        revision = self.store.freeze_revision(
            scope=self.scope,
            source_id=source_id,
            source_type="file",
            label=source_id,
            raw_content=text,
        )
        normalized = evidence.normalize_text(text)
        artifact = evidence.freeze_artifact(
            revision_id=revision["revision_id"],
            text=normalized,
            parser_name=chunking.PARSER_NAME,
            parser_version=chunking.PARSER_VERSION,
            config_hash="lineage",
            structure=chunking.artifact_structure(normalized),
        )
        self.store.store_artifact(scope=self.scope, artifact=artifact, chunks=chunking.chunk_text(normalized))
        start = normalized.index(needle)
        record = evidence.make_evidence(
            project_id=self.scope.project_id, artifact=artifact, spans=[(start, start + 6)]
        )
        self.store.register_evidence(record, scope=self.scope)
        return record.evidence_id

    def _commit(self, *, evidence_groups, key, base):
        origins = [
            {"derivation": "explicit", "evidence_refs": list(group)}
            for group in evidence_groups
        ]
        return self.store.commit_changes(
            actor_subject="alice",
            base_version=base,
            idempotency_key=key,
            changeset=kt.build_change_set(
                knowledge_space_id="local",
                project_id=self.scope.project_id,
                run_id=f"run-{key}",
                base_version=base,
                claims=[
                    {
                        "statement": "仅在当前低数据量场景使用 SQLite。",
                        "state": {
                            "knowledge_kind": "constraint",
                            "derivation": "explicit",
                            "epistemic_status": "asserted",
                        },
                        "origins": origins,
                        "support": ["evidence"],
                        "topic_ids": [],
                    }
                ],
            ),
            project_id=self.scope.project_id,
        )

    def test_two_reposts_of_one_article_are_one_witness(self):
        first = self._cite(SAME_TEXT, "src-report")
        repost = self._cite(SAME_TEXT, "src-repost")
        self.assertNotEqual(first, repost, "the two citations are different addresses")

        outcome = self._commit(evidence_groups=[(first,), (repost,)], key="repost-1", base=0)
        claim_id = outcome.created_claim_ids[0]
        version_id = self.store.get_claim(claim_id, self.scope)["current_version_id"]

        lineage = self.store.support_lineage(version_id, self.scope)
        self.assertEqual(lineage["group_count"], 2)
        self.assertEqual(
            lineage["independent_witnesses"],
            1,
            "identical content is one witness however many sources carry it",
        )
        self.assertEqual(lineage["repost_groups"], 1)
        groups = sorted(group["distinct_content"] for group in lineage["groups"])
        self.assertEqual(groups, [1, 1])

        reported = self.store.reposted_sources(self.scope)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["source_ids"], ["src-report", "src-repost"])

    def test_two_different_documents_are_two_witnesses(self):
        first = self._cite(SAME_TEXT, "src-a")
        second = self._cite(OTHER_TEXT, "src-b", needle="02:00")

        outcome = self._commit(evidence_groups=[(first,), (second,)], key="distinct-1", base=0)
        claim_id = outcome.created_claim_ids[0]
        version_id = self.store.get_claim(claim_id, self.scope)["current_version_id"]

        lineage = self.store.support_lineage(version_id, self.scope)
        self.assertEqual(lineage["group_count"], 2)
        self.assertEqual(lineage["independent_witnesses"], 2)
        self.assertEqual(lineage["repost_groups"], 0)
        self.assertEqual([], self.store.reposted_sources(self.scope))

    def test_one_group_citing_the_same_content_twice_counts_it_once(self):
        both = self._cite(SAME_TEXT, "src-double")
        outcome = self._commit(evidence_groups=[(both, both)], key="double-1", base=0)
        claim_id = outcome.created_claim_ids[0]
        version_id = self.store.get_claim(claim_id, self.scope)["current_version_id"]

        lineage = self.store.support_lineage(version_id, self.scope)
        group = lineage["groups"][0]
        self.assertEqual(len(group["requirements"]), 1, "a support group de-duplicates its own citation")
        self.assertEqual(group["duplicated_content"], 0)

    def test_lineage_reports_which_content_is_still_available(self):
        first = self._cite(SAME_TEXT, "src-live")
        repost = self._cite(SAME_TEXT, "src-withdrawn")
        self._commit(evidence_groups=[(first,), (repost,)], key="avail-1", base=0)
        claim_id = self.store.iter_claims(self.scope)[0]["claim_id"]
        version_id = self.store.get_claim(claim_id, self.scope)["current_version_id"]

        before = self.store.support_lineage(version_id, self.scope)
        self.assertTrue(all(group["intact"] for group in before["groups"]))
        self.assertEqual(before["intact_groups"], 2)

        self.store.withdraw_source("src-withdrawn", self.scope)
        after = self.store.support_lineage(version_id, self.scope)
        self.assertEqual(after["intact_groups"], 1)
        # Whichever group held the withdrawn source is the one that lost it.
        broken = [group for group in after["groups"] if not group["intact"]]
        self.assertEqual(len(broken), 1)
        self.assertEqual(len(broken[0]["unavailable"]), 1)
        self.assertFalse(broken[0]["unavailable"][0] == "")
        self.assertEqual(after["grounding_status"], "grounded", "the live witness still holds it")

    def test_an_unknown_citation_has_no_lineage_to_report(self):
        with self.assertRaises(Exception):
            self.store.evidence_lineage("evd_" + "0" * 64, self.scope)
        with self.assertRaises(Exception):
            self.store.support_lineage("clv_" + "0" * 32, self.scope)


if __name__ == "__main__":
    unittest.main()
