"""One definition of a secret path, applied by the CLI and by the corpus builder.

The rule exists so a private credential is never read. Two things it must get right,
and it got the first one wrong until real material exposed it.

A credential-named file one directory down was not excluded, because the delimiter
set before the word was `^`, `.`, `_` and `-` but not `/`. So `credentials.ts` was
skipped while `src/ai-service/credentials.ts` was read. That is the direction that
matters: the miss lets a secret in.

A rule that excludes too much is also a defect, because it teaches a reviewer to
ignore the exclusion report. `key` as a substring excludes `sankey_chart.svg`, which
is a chart.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))
sys.path.insert(0, str(REPO_ROOT / "evals" / "knowledge_v2"))

import build_corpus  # noqa: E402


def load_cli():
    spec = importlib.util.spec_from_file_location(
        "lw_cli_for_secret_rule", REPO_ROOT / "skills" / "lw" / "scripts" / "wiki.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = load_cli()

EXCLUDED = (
    "src/ai-service/credentials.ts",
    "ai-service/credentials.ts",
    "credentials.ts",
    "docs/credentials.md",
    "notes/credentials-cache.md",
    "docs/secret-notes.md",
    "config/.env",
    "config/.env.example",
    "keys/id_rsa",
    "certs/server.key",
    "vault/app.p12",
    "vault/app.pfx",
    "config/keystore.jks",
)

INCLUDED = (
    "charts/sankey_chart.svg",
    "docs/architecture.md",
    "worket/server/README.md",
    "src/keys.ts",
    "docs/keyboard-shortcuts.md",
    "src/credentialstore/format.py",
    "0825/BPv1.2.md",
)


class SecretRuleTests(unittest.TestCase):
    def test_the_cli_excludes_every_credential_shaped_path(self):
        for path in EXCLUDED:
            with self.subTest(path=path):
                self.assertTrue(CLI.is_secret_path(path), f"{path} would be read")

    def test_the_cli_reads_ordinary_project_files(self):
        for path in INCLUDED:
            with self.subTest(path=path):
                self.assertFalse(CLI.is_secret_path(path), f"{path} would be skipped")

    def test_the_corpus_builder_applies_the_same_rule(self):
        for path in EXCLUDED + INCLUDED:
            with self.subTest(path=path):
                self.assertEqual(
                    CLI.is_secret_path(path),
                    build_corpus.is_secret(path),
                    "the corpus builder and the CLI must agree about what a secret path is",
                )

    def test_a_word_containing_key_is_not_a_key_file(self):
        """The looser rule excluded a Sankey chart, which is how a report gets ignored."""

        self.assertNotIn("key", "charts/sankey_chart.svg".replace("sankey", ""))
        self.assertTrue(CLI.is_secret_path("keys/id_rsa"))
        self.assertFalse(CLI.is_secret_path("charts/sankey_chart.svg"))

    def test_both_pattern_lists_are_the_same_length(self):
        self.assertEqual(len(CLI.SECRET_PATTERNS), len(build_corpus.SECRET_PATTERNS))

    def test_the_rule_is_case_insensitive(self):
        for path in ("Config/.ENV", "docs/Credentials.md", "KEYS/ID_RSA"):
            with self.subTest(path=path):
                self.assertTrue(CLI.is_secret_path(path))


if __name__ == "__main__":
    unittest.main()
