"""The skill ships twice and nothing used to keep the copies honest.

`skills/lw/` is the copy an agent loads; `plugins/llm-wiki/skills/lw/` is the copy the
packaged MCP actually executes (see `llm_wiki_mcp.service.engine_path`). Both are edited
by hand, so a fix applied to one and not the other silently ships a different engine.
This walks both trees and fails on any difference.
"""

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

SOURCE = REPO_ROOT / "skills" / "lw"
DISTRIBUTED = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "lw"
IGNORED = {"__pycache__"}


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
            "sync them (the running MCP uses the plugin copy)",
        )

    def test_the_engine_the_mcp_runs_is_the_distributed_copy(self):
        from llm_wiki_mcp.service import engine_path

        self.assertEqual(
            engine_path().resolve(),
            (DISTRIBUTED / "scripts" / "wiki.py").resolve(),
            "the MCP must execute the distributed copy of the engine",
        )


if __name__ == "__main__":
    unittest.main()
