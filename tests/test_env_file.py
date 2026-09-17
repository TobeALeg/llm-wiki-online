"""The key file, and the rule that an exported variable beats a stored one.

The file exists so a key can be stored once instead of exported in every shell. It
is optional, it lives outside the repository, and a line the parser cannot read is
named rather than skipped, because a silently ignored line is a key the person
believes they configured and did not.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.model_roles import (  # noqa: E402
    ENV_FILE_NAME,
    credential_report,
    env_file_path,
    load_env_file,
)


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / ENV_FILE_NAME

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_a_missing_file_is_reported_and_not_an_error(self):
        summary = load_env_file(self.path, {})
        self.assertFalse(summary["present"])
        self.assertEqual(summary["loaded"], [])
        self.assertTrue(summary["path"].endswith(ENV_FILE_NAME))

    def test_the_file_is_read_into_the_environment(self):
        self._write("LLM_WIKI_API_KEY=sk-abc123\nLLM_WIKI_MODEL=some-model\n")
        environment = {}
        summary = load_env_file(self.path, environment)
        self.assertTrue(summary["present"])
        self.assertEqual(sorted(summary["loaded"]), ["LLM_WIKI_API_KEY", "LLM_WIKI_MODEL"])
        self.assertEqual(environment["LLM_WIKI_API_KEY"], "sk-abc123")
        self.assertEqual(environment["LLM_WIKI_MODEL"], "some-model")

    def test_an_exported_variable_beats_the_stored_one(self):
        self._write("LLM_WIKI_API_KEY=from-file\n")
        environment = {"LLM_WIKI_API_KEY": "from-shell"}
        summary = load_env_file(self.path, environment)
        self.assertEqual(environment["LLM_WIKI_API_KEY"], "from-shell")
        self.assertEqual(summary["already_set"], ["LLM_WIKI_API_KEY"])
        self.assertEqual(summary["loaded"], [])

    def test_override_reverses_that_on_request(self):
        self._write("LLM_WIKI_API_KEY=from-file\n")
        environment = {"LLM_WIKI_API_KEY": "from-shell"}
        load_env_file(self.path, environment, override=True)
        self.assertEqual(environment["LLM_WIKI_API_KEY"], "from-file")

    def test_comments_blanks_and_an_export_prefix_are_handled(self):
        self._write(
            "# a comment\n"
            "\n"
            "   \n"
            "export LLM_WIKI_MODEL=prefixed\n"
            "# LLM_WIKI_RENDER_MODEL=commented-out\n"
            "LLM_WIKI_BASE_URL=http://127.0.0.1:11434\n"
        )
        environment = {}
        summary = load_env_file(self.path, environment)
        self.assertEqual(environment["LLM_WIKI_MODEL"], "prefixed")
        self.assertEqual(environment["LLM_WIKI_BASE_URL"], "http://127.0.0.1:11434")
        self.assertNotIn("LLM_WIKI_RENDER_MODEL", environment)
        self.assertEqual(summary["ignored"], [])

    def test_quoted_values_lose_their_quotes(self):
        self._write('LLM_WIKI_API_KEY="sk-quoted"\nLLM_WIKI_MODEL=\'single\'\n')
        environment = {}
        load_env_file(self.path, environment)
        self.assertEqual(environment["LLM_WIKI_API_KEY"], "sk-quoted")
        self.assertEqual(environment["LLM_WIKI_MODEL"], "single")

    def test_a_value_may_contain_an_equals_sign(self):
        self._write("LLM_WIKI_BASE_URL=https://host/path?a=b\n")
        environment = {}
        load_env_file(self.path, environment)
        self.assertEqual(environment["LLM_WIKI_BASE_URL"], "https://host/path?a=b")

    def test_an_unparseable_line_is_named_rather_than_ignored(self):
        self._write("LLM_WIKI_API_KEY=sk-real\nthis is not an assignment\n-dash=value\n")
        environment = {}
        summary = load_env_file(self.path, environment)
        self.assertEqual(environment["LLM_WIKI_API_KEY"], "sk-real")
        self.assertEqual([item["line"] for item in summary["ignored"]], [2, 3])
        self.assertIn("this is not an assignment", summary["ignored"][0]["text"])

    def test_an_empty_value_is_still_set_so_it_can_mean_disabled(self):
        self._write("LLM_WIKI_RENDER_MODEL=\n")
        environment = {}
        load_env_file(self.path, environment)
        # Empty means "not overridden", which is what resolve_model reads it as.
        self.assertEqual(environment["LLM_WIKI_RENDER_MODEL"], "")


class EnvFilePathTests(unittest.TestCase):
    def test_the_default_is_beside_the_local_knowledge_database(self):
        self.assertEqual(env_file_path({}), Path.home() / ".llm-wiki" / ENV_FILE_NAME)

    def test_the_home_variable_moves_it(self):
        path = env_file_path({"LLM_WIKI_HOME": "/tmp/somewhere"})
        self.assertEqual(path, Path("/tmp/somewhere") / ENV_FILE_NAME)

    def test_an_empty_home_variable_falls_back_to_the_default(self):
        self.assertEqual(env_file_path({"LLM_WIKI_HOME": "   "}), Path.home() / ".llm-wiki" / ENV_FILE_NAME)


class CredentialReportTests(unittest.TestCase):
    def test_a_missing_key_is_reported_without_inventing_one(self):
        report = credential_report({})
        self.assertFalse(report["key_present"])
        self.assertEqual(report["key_variable"], "")
        self.assertEqual(report["key_length"], 0)
        self.assertEqual(report["key_tail"], "")

    def test_the_report_confirms_the_key_without_printing_it(self):
        secret = "sk-abcdefghijklmnop1234"
        report = credential_report({"LLM_WIKI_API_KEY": secret})
        self.assertTrue(report["key_present"])
        self.assertEqual(report["key_variable"], "LLM_WIKI_API_KEY")
        self.assertEqual(report["key_length"], len(secret))
        self.assertEqual(report["key_tail"], "1234")
        self.assertNotIn(secret, str(report), "the report must not carry the secret")

    def test_the_wiki_variable_wins_over_the_vendor_variable(self):
        report = credential_report({"LLM_WIKI_API_KEY": "one", "DEEPSEEK_API_KEY": "two"})
        self.assertEqual(report["key_variable"], "LLM_WIKI_API_KEY")

    def test_the_vendor_variable_is_used_when_the_wiki_one_is_absent(self):
        report = credential_report({"DEEPSEEK_API_KEY": "two"})
        self.assertEqual(report["key_variable"], "DEEPSEEK_API_KEY")

    def test_whitespace_is_not_a_key(self):
        report = credential_report({"LLM_WIKI_API_KEY": "   "})
        self.assertFalse(report["key_present"])

    def test_the_report_names_the_model_each_role_will_use(self):
        report = credential_report(
            {"LLM_WIKI_API_KEY": "k", "LLM_WIKI_MODEL": "base", "LLM_WIKI_DISCOVERY_MODEL": "cheap"}
        )
        self.assertEqual(report["models"]["discovery"]["model"], "cheap")
        self.assertEqual(report["models"]["discovery"]["source"], "LLM_WIKI_DISCOVERY_MODEL")
        self.assertEqual(report["models"]["reasoning"]["model"], "base")
        self.assertEqual(report["models"]["grounding"]["model"], "base")

    def test_the_report_carries_the_base_url_without_a_trailing_slash(self):
        report = credential_report({"LLM_WIKI_API_KEY": "k", "LLM_WIKI_BASE_URL": "http://127.0.0.1:11434/"})
        self.assertEqual(report["base_url"], "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
