"""Two grounding heuristics that read a question as an assertion, and a scope too widely.

The denial detector reads a bare 不 near a term as negating it. In an A-not-A
question form (会不会, 能不能, 是不是, 有没有) that 不 or 没 turns a statement into a
question rather than negating anything, so material that asks whether two things
conflict was read as material that denies a conflict. A citation into that material
was then refused as contradicted, which loses knowledge the project did state, in a
direction that looks like a strict gate.

The qualifier check had the opposite shape: it required every candidate to repeat
every qualifier anywhere in its chunk, so a 除非 belonging to a sentence about
Postgres was demanded of a sentence about something else.

Both were found by running the extraction against a real provider. Every controlled
fake returned wording that happened to avoid them.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import knowledge_pipeline as pipeline  # noqa: E402
from llm_wiki_mcp.knowledge_types import ClaimCandidate, ClaimState  # noqa: E402


def candidate(statement, conditions=(), kind="constraint"):
    return ClaimCandidate(
        statement=statement,
        state=ClaimState(
            knowledge_kind=kind, derivation="explicit", epistemic_status="asserted"
        ),
        conditions=tuple(conditions),
    )


def cited(statement):
    """A candidate that cites something, for the checks that need a citation."""

    return ClaimCandidate(
        statement=statement,
        state=ClaimState(
            knowledge_kind="fact", derivation="explicit", epistemic_status="asserted"
        ),
        evidence_refs=("evd_" + "a" * 64,),
    )


class QuestionFormTests(unittest.TestCase):
    def test_an_a_not_a_question_is_not_a_negation(self):
        for text in (
            "这个缓存会不会打架？",
            "缓存是不是要换？",
            "缓存有没有上限？",
            "这个方案能不能落地？",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    pipeline._negation_near(text, "缓存"),
                    "a question form must not read as a negation",
                )

    def test_a_real_negation_of_the_same_verb_still_counts(self):
        for text in ("缓存不会打架。", "缓存不再使用。", "缓存层不使用 Redis。"):
            with self.subTest(text=text):
                self.assertTrue(pipeline._negation_near(text, "缓存"))

    def test_the_a_not_a_form_does_not_hide_a_negation_elsewhere_in_the_sentence(self):
        # The question form is neutralised, the trailing 不 is not.
        self.assertTrue(pipeline._negation_near("缓存能不能用还不确定。", "缓存"))

    def test_a_negation_before_the_term_still_counts(self):
        self.assertTrue(pipeline._negation_near("文档里没有记录这个原因。", "原因"))

    def test_the_exact_sentence_from_the_real_run_is_no_longer_a_denial(self):
        """The sentence a real model's citation was refused against."""

        material = "工程师问了一句这个缓存怎么失效，以及和现有镜像层缓存会不会打架。"
        self.assertFalse(pipeline._negation_near(material, "缓存"))

    def test_the_pattern_is_named_and_matches_both_neutral_syllables(self):
        self.assertTrue(hasattr(pipeline, "ANOT_A_RE"))
        self.assertIsNotNone(pipeline.ANOT_A_RE.search("会不会"))
        self.assertIsNotNone(pipeline.ANOT_A_RE.search("有没有"))
        self.assertIsNone(pipeline.ANOT_A_RE.search("不会"))


class QualifierScopeTests(unittest.TestCase):
    """The qualifier rule is deliberately material-wide.

    Narrowing it to the qualifier's own sentence was tried and rejected. A statement
    can rewrite its subject closely enough that no term-level rule separates it from
    an unrelated qualifier a paragraph away, and the direction that fails publishes a
    scoped statement as a universal one. So the rule stays broad, and the cost is a
    false positive on a statement that shares a subject with a qualified sentence
    elsewhere, paid in REVIEW rather than by publishing or discarding.
    """

    MATERIAL = (
        "新平台迁移仅限当前项目，暂不迁移生产环境的写入路径。"
        + chr(10)
        + "该方案可能是长期方向，除非引入双写机制。"
    )

    def test_a_statement_that_drops_the_qualifiers_reports_every_one(self):
        self.assertEqual(
            ["仅限当前项目", "暂不", "可能", "除非"],
            pipeline._missing_qualifiers(
                candidate("新平台迁移适用于所有生产环境"), self.MATERIAL
            ),
        )

    def test_a_statement_that_carries_them_reports_nothing(self):
        self.assertEqual(
            [],
            pipeline._missing_qualifiers(
                candidate(
                    "新平台迁移仅限当前项目，暂不迁移生产环境的写入路径",
                    (
                        "仅限当前项目",
                        "暂不迁移生产环境的写入路径",
                        "可能是长期方向",
                        "除非引入双写机制",
                    ),
                ),
                self.MATERIAL,
            ),
        )

    def test_a_statement_carrying_only_some_qualifiers_reports_the_rest(self):
        """The rule is material-wide, so carrying one does not excuse the others."""

        self.assertEqual(
            ["仅限当前项目", "可能", "除非"],
            pipeline._missing_qualifiers(
                candidate("暂不迁移生产环境的写入路径"), self.MATERIAL
            ),
        )


class AttributeKindTests(unittest.TestCase):
    """A stray attributes key is a formatting slip, not a false statement.

    Observed on a real run: a faithful question was rejected outright because the
    model attached a steps payload to it. Nothing reads that key for an
    open_question, so dropping it keeps the statement and loses nothing.
    """

    def _build(self, kind, attributes):
        return pipeline._build_candidate(
            {
                "statement": "这个缓存会不会打架？",
                "knowledge_kind": kind,
                "derivation": "explicit",
                "epistemic_status": "asserted",
                "attributes": attributes,
            },
            batch_id="batch-001",
        )

    def test_a_payload_the_kind_does_not_define_is_dropped(self):
        built = self._build("open_question", {"steps": [{"action": "x"}]})
        self.assertEqual({}, dict(built.attributes))
        self.assertEqual("open_question", built.state.knowledge_kind)

    def test_a_payload_the_kind_does_not_define_is_reported(self):
        _kept, dropped = pipeline._declared_attributes(
            {"knowledge_kind": "open_question", "attributes": {"steps": [{"action": "x"}]}}
        )
        self.assertEqual(["steps"], dropped)

    def test_a_payload_the_kind_does_define_is_kept(self):
        built = self._build("process", {"steps": [{"action": "x"}]})
        self.assertEqual(["steps"], sorted(dict(built.attributes)))

    def test_an_extra_key_inside_a_defined_payload_is_dropped(self):
        kept, dropped = pipeline._declared_attributes(
            {"knowledge_kind": "process", "attributes": {"steps": [], "invented": 1}}
        )
        self.assertEqual(["steps"], sorted(kept))
        self.assertEqual(["invented"], dropped)

    def test_the_defined_kinds_match_the_contract(self):
        from llm_wiki_mcp.knowledge_types import KNOWLEDGE_KINDS

        self.assertEqual(("process", "decision", "architecture"), pipeline.ATTRIBUTE_KINDS)
        self.assertTrue(set(pipeline.ATTRIBUTE_KINDS) <= set(KNOWLEDGE_KINDS))

    def test_an_empty_payload_stays_empty(self):
        kept, dropped = pipeline._declared_attributes({"knowledge_kind": "process", "attributes": {}})
        self.assertEqual({}, kept)
        self.assertEqual([], dropped)


class DenialScopeTests(unittest.TestCase):
    """Proximity cannot tell a denial from an unrelated negative clause.

    Observed on real material: a statement about a contract clause under legal review
    was refused against the sentence that begins with it and continues into an
    unrelated negative clause. Containment settles it; proximity does not.
    """

    def test_a_sentence_containing_the_statement_is_not_a_denial(self):
        for statement, material in (
            ("合同的自动续期条款目前仍在法务审阅中。", "合同的自动续期条款目前仍在法务审阅中，尚未签署。"),
            ("讨论就到这里。", "没有人说采用，也没有人说不采用，讨论就到这里。"),
        ):
            with self.subTest(statement=statement):
                self.assertEqual("", pipeline._denial(cited(statement), material))

    def test_a_sentence_stating_the_opposite_is_still_a_denial(self):
        for statement, material in (
            ("缓存层使用 Redis。", "缓存层不使用 Redis，改用 memcached。"),
            ("项目引入 Postgres。", "我们暂不引入 Postgres。"),
        ):
            with self.subTest(statement=statement):
                self.assertNotEqual("", pipeline._denial(cited(statement), material))

    def test_a_lowercased_term_is_found_in_capitalised_material(self):
        """Terms are lowercased at extraction and product names keep their capitals."""

        self.assertTrue(pipeline._negation_near("缓存层不使用 Redis。", "redis"))
        self.assertFalse(pipeline._negation_near("缓存层使用 Redis。", "redis"))

    def test_the_a_not_a_replacement_keeps_the_text_around_it(self):
        self.assertEqual("会会", pipeline.ANOT_A_RE.sub(pipeline._NOT_A_NOT, "会不会"))
        self.assertEqual("有有", pipeline.ANOT_A_RE.sub(pipeline._NOT_A_NOT, "有没有"))


class ReviewQuestionLanguageTests(unittest.TestCase):
    """A review question is what a person acts on, so it is in their language.

    Found by running a Chinese business plan through the real pipeline: every review
    it opened asked its question in English. The mirror case matters too, so the
    choice is made from the statement being reviewed rather than hard-coded.
    """

    def test_a_chinese_statement_gets_a_chinese_question(self):
        question = pipeline.review_question("insufficient_context", "企业无需从零建模。")
        self.assertIn("材料", question)
        self.assertNotIn("proposition", question)

    def test_an_english_statement_gets_an_english_question(self):
        question = pipeline.review_question(
            "insufficient_context", "The harness may own deployment."
        )
        self.assertIn("proposition", question)

    def test_a_statement_with_a_product_name_still_counts_as_chinese(self):
        question = pipeline.review_question("ambiguous_adoption", "助手建议用 SQLite 做缓存。")
        self.assertIn("采纳", question)

    def test_an_empty_statement_falls_back_to_english(self):
        self.assertEqual("en", pipeline.statement_language(""))
        self.assertEqual("en", pipeline.statement_language("   "))

    def test_every_trigger_code_has_both_languages(self):
        for code, templates in pipeline.REVIEW_QUESTIONS.items():
            with self.subTest(code=code):
                self.assertEqual({"zh", "en"}, set(templates))
        self.assertEqual({"zh", "en"}, set(pipeline.REVIEW_QUESTION_DEFAULT))

    def test_an_unknown_trigger_falls_back_rather_than_returning_nothing(self):
        question = pipeline.review_question("something_new", "企业无需从零建模。")
        self.assertEqual(pipeline.REVIEW_QUESTION_DEFAULT["zh"], question)

    def test_the_missing_qualifier_case_carries_the_qualifiers_it_names(self):
        question = pipeline.review_question(
            "missing_qualifier", "新平台迁移适用于所有生产环境", detail="原文限定：仅限当前项目"
        )
        self.assertIn("仅限当前项目", question)
        self.assertIn("限定", question)

    def test_the_disposition_route_uses_the_same_helper(self):
        from llm_wiki_mcp.knowledge_types import ClaimCandidate, ClaimState

        candidate = ClaimCandidate(
            statement="企业无需从零建模。",
            state=ClaimState(
                knowledge_kind="judgment", derivation="explicit", epistemic_status="asserted"
            ),
            reason_codes=("insufficient_context",),
        )
        self.assertIn("材料", pipeline._review_question(candidate))


if __name__ == "__main__":
    unittest.main()
