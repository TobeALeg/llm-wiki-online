"""A template that carries a real key leaks it the moment anyone commits.

The template is tracked on purpose, so `git diff` shows it and reviewers read it.
That is exactly why a key must never be typed into it: one `git add -A` puts a live
credential in history, where deleting the line no longer removes it. The real file
lives in `$LLM_WIKI_HOME/env`, outside the repository, and this test keeps the two
from being confused.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

TEMPLATE = REPO_ROOT / "docs" / "knowledge-v2" / "env.example"

SECRET_SHAPES = (
    ("provider key prefix", re.compile(r"\bsk-[A-Za-z0-9]{8,}")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}")),
    ("long hex run", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("long base64-ish run", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
)


class EnvTemplateTests(unittest.TestCase):
    def test_the_template_is_tracked_so_it_must_stay_empty(self):
        """If it ever stops being tracked, this guard no longer protects anything."""

        import subprocess

        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(TEMPLATE.relative_to(REPO_ROOT))],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, "env.example must stay tracked")

    SECRET_VARIABLES = ("LLM_WIKI_API_KEY", "DEEPSEEK_API_KEY")
    """Only these must be empty. A model name and a public endpoint are defaults
    worth shipping in the template; a credential is not."""

    def test_every_credential_variable_is_empty_in_the_template(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        found = {
            line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
            for line in text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        for name in self.SECRET_VARIABLES:
            if name not in found:
                continue
            with self.subTest(variable=name):
                self.assertEqual(
                    found[name],
                    "",
                    f"{name} has a value in the template. Put the real value in "
                    "$LLM_WIKI_HOME/env, which is outside the repository.",
                )

    def test_the_template_does_ship_a_usable_default_model_and_endpoint(self):
        """A template with nothing filled in would send a reader hunting."""

        text = TEMPLATE.read_text(encoding="utf-8")
        found = {
            line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
            for line in text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        self.assertNotEqual(found.get("LLM_WIKI_MODEL", ""), "")
        self.assertTrue(found.get("LLM_WIKI_BASE_URL", "").startswith("http"))

    def test_the_template_declares_every_variable_the_code_reads(self):
        from llm_wiki_mcp.model_roles import DEFAULT_MODEL_ENV, MODEL_ROLE_ENV

        text = TEMPLATE.read_text(encoding="utf-8")
        declared = {
            line.split("=", 1)[0].strip()
            for line in text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        self.assertIn(DEFAULT_MODEL_ENV, declared)
        for variable in sorted(set(MODEL_ROLE_ENV.values())):
            with self.subTest(variable=variable):
                self.assertIn(variable, declared)

    def test_the_template_carries_no_provider_key_shape_anywhere(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for name, pattern in SECRET_SHAPES:
            with self.subTest(shape=name):
                self.assertIsNone(
                    pattern.search(text),
                    f"the template matches a {name} shape",
                )

    def test_the_repository_ignores_a_local_env_file_if_one_appears(self):
        """A stray `.env` in a checkout must not be committable."""

        import subprocess

        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, "a checkout-local .env must be ignored")


if __name__ == "__main__":
    unittest.main()
