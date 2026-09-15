import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "llm-wiki"
sys.path.insert(0, str(PLUGIN_ROOT))
service = importlib.import_module("llm_wiki_mcp.service")


class McpServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.registry = Path(self.temporary.name) / "projects.json"
        self.environment = mock.patch.dict(
            os.environ, {"LLM_WIKI_REGISTRY": str(self.registry)}, clear=False
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_registry_exposes_ids_without_local_paths(self):
        service.register_project("demo", str(self.root), "Demo")
        projects = service.list_projects()
        self.assertEqual(projects[0]["id"], "demo")
        self.assertNotIn("path", projects[0])

    def test_unknown_project_cannot_select_arbitrary_path(self):
        with self.assertRaises(service.ServiceError):
            service.project_root("not-allowed")

    def test_init_and_episode_use_existing_engine(self):
        service.register_project("demo", str(self.root))
        service.run_wiki("demo", "init", timeout=30)
        episode = service.episode_json("Decision", "Use SQLite.", tags=["storage"])
        result = service.run_wiki("demo", "ingest", ["--episode", episode], timeout=30)
        self.assertTrue(result["ok"])
        self.assertEqual(len(list((self.root / ".llm-wiki" / "episodes").glob("*.json"))), 1)

    def test_page_slug_rejects_traversal(self):
        service.register_project("demo", str(self.root))
        with self.assertRaises(service.ServiceError):
            service.read_page("demo", "../secret")


if __name__ == "__main__":
    unittest.main()
