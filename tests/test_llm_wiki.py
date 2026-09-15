import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "skills" / "lw" / "scripts" / "wiki.py"
SPEC = importlib.util.spec_from_file_location("llm_wiki", SCRIPT)
wiki = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(wiki)


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        wiki.init_wiki(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_scan_finds_text_and_excludes_secrets_and_dependencies(self):
        (self.root / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        dependency = self.root / "node_modules" / "x"
        dependency.mkdir(parents=True)
        (dependency / "index.js").write_text("ignored", encoding="utf-8")
        records = wiki.scan_records(self.root)
        self.assertEqual(set(records), {"app.py"})
        diff = wiki.diff_records(wiki.load_state(self.root), records)
        self.assertEqual(diff["added"], ["app.py"])

    def test_ingest_is_immutable_and_deduplicated(self):
        first = wiki.ingest_episode(self.root, "Remember the API contract.", None)
        second = wiki.ingest_episode(self.root, "Remember the API contract.", None)
        self.assertEqual(first, second)
        episodes = list((self.root / ".llm-wiki" / "episodes").glob("*.json"))
        self.assertEqual(len(episodes), 1)
        self.assertEqual(json.loads(episodes[0].read_text())["summary"], "Remember the API contract.")

    def test_update_applies_valid_model_page_and_provenance(self):
        (self.root / "architecture.md").write_text("Use SQLite for the index.\n", encoding="utf-8")

        def fake_model(bundle, purpose, pages):
            source = bundle["files"][0]["source_id"]
            return {
                "pages": [{
                    "slug": "local-index",
                    "title": "Local index",
                    "type": "decision",
                    "status": "current",
                    "tags": ["storage"],
                    "summary": "SQLite is the initial local index.",
                    "body": "The project uses SQLite for its first local index.",
                    "sources": [source],
                }],
                "note": "Recorded the storage decision.",
                "_provider": {"base_url": "mock", "model": "deepseek-flash"},
            }

        args = type("Args", (), {"episode": None, "episode_file": None})()
        with mock.patch.object(wiki, "call_model", side_effect=fake_model):
            wiki.do_update(self.root, args)
        page = (self.root / ".llm-wiki" / "pages" / "local-index.md").read_text()
        self.assertIn("file:architecture.md@sha256:", page)
        self.assertIn("Local index", (self.root / ".llm-wiki" / "index.md").read_text())
        self.assertEqual(wiki.lint(self.root), [])

    def test_rejects_path_traversal_slug(self):
        with self.assertRaises(wiki.WikiError):
            wiki.validate_page({
                "slug": "../outside",
                "title": "Bad",
                "type": "guide",
                "status": "current",
                "tags": [],
                "summary": "bad",
                "body": "bad",
                "sources": ["episode:test"],
            }, {"episode:test"})


if __name__ == "__main__":
    unittest.main()
