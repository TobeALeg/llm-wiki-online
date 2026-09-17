"""The skill ships twice and nothing used to keep the copies honest.

`skills/lw/` is the copy an agent loads; `plugins/llm-wiki/skills/lw/` is the copy the
packaged MCP actually executes (see `llm_wiki_mcp.service.engine_path`). Both are edited
by hand, so a fix applied to one and not the other silently ships a different engine.
This walks both trees and fails on any difference.

The v2 core ships a third time inside the server package, at
`llm_wiki_mcp/<module>.py`, for the same reason: the server imports the one
implementation rather than growing a second. `scripts/sync_skill_distribution.py`
generates the vendored copies and its module list is read here, so the check and the
generator cannot disagree about which files are owned.
"""

import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

SOURCE = REPO_ROOT / "skills" / "lw"
DISTRIBUTED = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "lw"
PACKAGE = REPO_ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp"
IGNORED = {"__pycache__"}


def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_skill_distribution", REPO_ROOT / "scripts" / "sync_skill_distribution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SYNC = _load_sync_module()


def relative_files(root):
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not IGNORED & set(path.relative_to(root).parts)
    )


class SkillDistributionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_dir(), f"missing {SOURCE}")
        self.assertTrue(DISTRIBUTED.is_dir(), f"missing {DISTRIBUTED}")

    def test_every_skill_file_exists_in_both_copies(self):
        self.assertEqual(
            relative_files(SOURCE),
            relative_files(DISTRIBUTED),
            "the shipped skill tree and the repo skill tree list different files",
        )

    def test_every_skill_file_is_byte_identical_in_both_copies(self):
        differing = [
            name
            for name in relative_files(SOURCE)
            if (SOURCE / name).read_bytes() != (DISTRIBUTED / name).read_bytes()
        ]
        self.assertEqual(
            differing,
            [],
            "these files differ between skills/lw and plugins/llm-wiki/skills/lw; "
            "run scripts/sync_skill_distribution.py (the running MCP uses the plugin copy)",
        )

    def test_the_generated_copies_match_their_canonical_sources(self):
        stale = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in SYNC._stale(SYNC._plan())
        ]
        self.assertEqual(
            stale,
            [],
            "these generated files are out of date; run scripts/sync_skill_distribution.py",
        )

    def test_the_engine_the_mcp_runs_is_the_distributed_copy(self):
        from llm_wiki_mcp.service import engine_path

        self.assertEqual(
            engine_path().resolve(),
            (DISTRIBUTED / "scripts" / "wiki.py").resolve(),
            "the MCP must execute the distributed copy of the engine",
        )

    def test_every_vendored_core_module_matches_both_skill_copies(self):
        for name in SYNC.VENDORED_MODULES:
            canonical = PACKAGE / name
            if not canonical.exists():
                continue
            with self.subTest(module=name):
                packaged = canonical.read_bytes()
                for tree in (SOURCE, DISTRIBUTED):
                    shipped = (tree / "scripts" / name).read_bytes()
                    self.assertEqual(
                        packaged,
                        shipped,
                        f"llm_wiki_mcp/{name} differs from {tree}/scripts/{name}; "
                        "the server and the CLI must run one implementation, not two",
                    )

    def test_every_vendored_core_module_is_standard_library_only(self):
        """A clean skill install has no plugin dependencies available.

        The vendored copies are what `/lw` loads when nothing was pip-installed, so a
        third-party import here would only surface on a machine that never ran the dev
        setup, which is the one place the suite cannot reach.
        """

        siblings = {name.removesuffix(".py") for name in SYNC.VENDORED_MODULES}
        allowed = {
            "__future__",
            "collections",
            "contextlib",
            "dataclasses",
            "datetime",
            "hashlib",
            "json",
            "pathlib",
            "re",
            "sqlite3",
            "typing",
            "unicodedata",
            "uuid",
        }
        for name in SYNC.VENDORED_MODULES:
            canonical = PACKAGE / name
            if not canonical.exists():
                continue
            with self.subTest(module=name):
                imported = set()
                for line in canonical.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    module = ""
                    if stripped.startswith("import "):
                        module = stripped[len("import "):].split()[0]
                    elif stripped.startswith("from ") and " import " in stripped:
                        module = stripped[len("from "):].split(" import ")[0].strip()
                    if not module:
                        continue
                    # A relative import, and a vendored sibling imported flat so it
                    # works without a package, both travel with this file.
                    if module.startswith("."):
                        continue
                    root = module.split(".")[0]
                    if root in siblings:
                        continue
                    imported.add(root)
                self.assertEqual(
                    sorted(imported - allowed),
                    [],
                    f"{name} imports something outside the standard-library allowlist",
                )

    def test_the_server_can_import_the_chunker_as_a_module(self):
        import llm_wiki_mcp.chunking as module

        text = "# Title\n\nBody text.\n"
        chunks = module.chunk_text(text)
        self.assertTrue(chunks)
        self.assertEqual([chunk.text for chunk in chunks], [text[chunk.start:chunk.end] for chunk in chunks])


if __name__ == "__main__":
    unittest.main()
