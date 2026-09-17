"""The machine's config directory must not look like a project.

`$HOME/.llm-wiki` holds the key file, the project registry and the local knowledge
database. It shares a name with the project marker `find_root` looks for, so
creating it used to make the home directory resolve as the project root for every
unmarked directory beneath it. A CLI run from a temp checkout then read and wrote
the user's home instead of the checkout. This pins the distinction.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).parents[1]
WIKI_SCRIPT = REPO_ROOT / "skills" / "lw" / "scripts" / "wiki.py"
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.model_roles import ENV_FILE_NAME, env_file_path, load_env_file  # noqa: E402

SPEC = importlib.util.spec_from_file_location("lw_cli_root", WIKI_SCRIPT)
wiki = importlib.util.module_from_spec(SPEC)
sys.modules["lw_cli_root"] = wiki
SPEC.loader.exec_module(wiki)


class FindRootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.project = self.home / "work" / "checkout"
        self.project.mkdir(parents=True)
        self.config = self.home / ".llm-wiki"
        self.config.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def _run_from(self, directory):
        previous = Path.cwd()
        os.chdir(directory)
        try:
            with mock.patch.object(Path, "home", staticmethod(lambda: self.home)):
                return wiki.find_root(None)
        finally:
            os.chdir(previous)

    def test_the_config_directory_does_not_make_the_home_directory_a_project_root(self):
        resolved = self._run_from(self.project)
        self.assertEqual(resolved, self.project, "an unmarked directory is its own root")

    def test_a_project_marker_below_the_home_directory_still_wins(self):
        (self.project / ".llm-wiki").mkdir()
        self.assertEqual(self._run_from(self.project), self.project)

        nested = self.project / "src" / "deep"
        nested.mkdir(parents=True)
        self.assertEqual(self._run_from(nested), self.project)

    def test_a_project_rooted_at_the_home_directory_still_resolves(self):
        """The walk never escapes home, so a command run at home finds home."""

        (self.home / ".git").mkdir()
        self.assertEqual(self._run_from(self.home), self.home)
        nested = self.home / "scratch" / "here"
        nested.mkdir(parents=True)
        self.assertEqual(self._run_from(nested), nested)

    def test_an_explicit_root_is_never_second_guessed(self):
        with mock.patch.object(Path, "home", staticmethod(lambda: self.home)):
            self.assertEqual(wiki.find_root(str(self.project)), self.project)

    def test_the_configured_home_does_not_change_the_project_root(self):
        """`LLM_WIKI_HOME` moves the config; it must not move the project."""

        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        with mock.patch.dict(os.environ, {"LLM_WIKI_HOME": str(elsewhere)}, clear=False):
            self.assertEqual(self._run_from(self.project), self.project)


class EnvFileLocationTests(unittest.TestCase):
    def test_the_default_location_is_the_config_directory(self):
        self.assertEqual(env_file_path({}), Path.home() / ".llm-wiki" / ENV_FILE_NAME)

    def test_the_location_follows_the_home_variable(self):
        self.assertEqual(env_file_path({"LLM_WIKI_HOME": "/tmp/x"}), Path("/tmp/x") / ENV_FILE_NAME)

    def test_a_filled_env_file_is_read_and_the_key_becomes_visible(self):
        """The whole point of the file: one place to put the key."""

        temporary = Path(tempfile.mkdtemp())
        target = temporary / ENV_FILE_NAME
        target.write_text("LLM_WIKI_API_KEY=sk-filled-in\n", encoding="utf-8")
        environment: dict = {}
        summary = load_env_file(target, environment)
        self.assertEqual(summary["loaded"], ["LLM_WIKI_API_KEY"])
        self.assertEqual(environment["LLM_WIKI_API_KEY"], "sk-filled-in")

    def test_the_repository_does_not_ship_a_filled_key(self):
        for path in (REPO_ROOT / ".env", REPO_ROOT / "env", REPO_ROOT / "docs" / "knowledge-v2" / ENV_FILE_NAME):
            with self.subTest(path=str(path)):
                self.assertFalse(path.exists(), "a real key file must never be committed")

    def test_the_committed_template_carries_no_secret(self):
        template = (REPO_ROOT / "docs" / "knowledge-v2" / "env.example").read_text(encoding="utf-8")
        for line in template.splitlines():
            stripped = line.strip()
            if not stripped.startswith("LLM_WIKI_API_KEY="):
                continue
            self.assertEqual(stripped, "LLM_WIKI_API_KEY=", "the template must ship an empty key")

    def test_the_home_config_file_is_never_a_committed_path(self):
        """It lives outside the repository by construction, so nothing to ignore."""

        resolved = env_file_path({})
        self.assertNotEqual(resolved, REPO_ROOT / ENV_FILE_NAME)
        self.assertFalse(str(resolved).startswith(str(REPO_ROOT)))


if __name__ == "__main__":
    unittest.main()
