"""A question is not a denial.

The denial detector reads a bare 不 near a term as negating it. In an A-not-A
question form (会不会, 能不能, 是不是, 有没有) that 不 or 没 turns a statement into a
question rather than negating anything, so material that asks whether two things
conflict was being read as material that denies a conflict. A citation into that
material was then refused as contradicted, which loses knowledge the project did
state, in a direction that looks like a strict gate.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import knowledge_pipeline as pipeline  # noqa: E402


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
        for text in (
            "缓存不会打架。",
            "缓存不再使用。",
            "数据库不引入 Postgres。",
            "缓存层不使用 Redis。",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    pipeline._negation_near(text, "缓存" if "缓存" in text else "数据库"),
                    "a genuine negation keeps its meaning",
                )

    def test_the_a_not_a_form_does_not_hide_a_negation_elsewhere_in_the_sentence(self):
        # The question is neutralised, the trailing 不 is not.
        self.assertTrue(pipeline._negation_near("缓存能不能用还不确定。", "缓存"))

    def test_a_negation_before_the_term_still_counts(self):
        self.assertTrue(pipeline._negation_near("文档里没有记录这个原因。", "原因"))

    def test_the_longer_material_from_the_real_run_is_no_longer_a_denial(self):
        """The exact sentence the real model's citation was refused against."""

        material = "工程师问了一句这个缓存怎么失效，以及和现有镜像层缓存会不会打架。"
        self.assertFalse(pipeline._negation_near(material, "缓存"))

    def test_the_pattern_is_named_and_the_docstring_says_why(self):
        self.assertTrue(hasattr(pipeline, "ANOT_A_RE"))
        self.assertIsNotNone(pipeline.ANOT_A_RE.search("会不会"))
        self.assertIsNotNone(pipeline.ANOT_A_RE.search("有没有"))
        self.assertIsNone(pipeline.ANOT_A_RE.search("不会"))


if __name__ == "__main__":
    unittest.main()
