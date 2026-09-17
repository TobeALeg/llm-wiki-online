"""Regenerate the vendored skill copies from their canonical sources.

The v2 core ships three times on purpose. It is the plugin package, and it is
vendored into the skill directory twice, so `/lw` works on a machine that never
installed the plugin. Byte equality is the contract between the copies, which
makes this script the only thing allowed to write them. Running it by hand is
what keeps the copies from drifting into three different programs.

Canonical inputs:

- ``plugins/llm-wiki/llm_wiki_mcp/<module>.py`` for each vendored core module
- ``skills/lw/`` for the whole skill tree

Generated outputs:

- ``skills/lw/scripts/<module>.py``
- ``plugins/llm-wiki/skills/lw/``

Usage::

    python scripts/sync_skill_distribution.py          # write the copies
    python scripts/sync_skill_distribution.py --check  # fail on drift, write nothing
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp"
SKILL_SOURCE = REPO_ROOT / "skills" / "lw"
SKILL_MIRROR = REPO_ROOT / "plugins" / "llm-wiki" / "skills" / "lw"

VENDORED_MODULES = (
    "chunking.py",
    "knowledge_types.py",
    "evidence.py",
    "claim_store.py",
    "projection.py",
    "retrieval.py",
    "wiki_prompts.py",
)
"""Core modules that must exist inside the skill package verbatim.

Each one is standard library only, so a clean skill install can import it
without the plugin's `mcp` dependency.
"""

IGNORED_DIRECTORIES = {"__pycache__"}


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not IGNORED_DIRECTORIES.intersection(path.parts)
    )


def _drift(expected: Path, actual: Path) -> bool:
    if not actual.exists():
        return True
    return not filecmp.cmp(expected, actual, shallow=False)


def _plan() -> list[tuple[Path, Path]]:
    """Every (source, destination) pair this script owns.

    Order matters. Vendored modules land in the skill tree first, then the whole
    skill tree mirrors into the plugin, so the mirror never reads a file this
    same run has not written yet.
    """

    plan: list[tuple[Path, Path]] = []
    for name in VENDORED_MODULES:
        source = PACKAGE / name
        if not source.exists():
            continue
        plan.append((source, SKILL_SOURCE / "scripts" / name))

    skill_files = set(_files(SKILL_SOURCE))
    skill_files.update(
        destination for _, destination in plan if destination.is_relative_to(SKILL_SOURCE)
    )
    for source in sorted(skill_files):
        plan.append((source, SKILL_MIRROR / source.relative_to(SKILL_SOURCE)))
    return plan


def _stale(plan: list[tuple[Path, Path]]) -> list[Path]:
    return [destination for source, destination in plan if _drift(source, destination)]


def _orphans(plan: list[tuple[Path, Path]]) -> list[Path]:
    """Generated files with no canonical source left, which must be removed."""

    owned = {destination for _, destination in plan}
    stale: list[Path] = []
    for root in (SKILL_SOURCE / "scripts", SKILL_MIRROR):
        for path in _files(root):
            if path in owned:
                continue
            if path.name in VENDORED_MODULES or path.name == "chunking.py":
                stale.append(path)
    return stale


def check() -> int:
    plan = _plan()
    problems = [*_stale(plan), *_orphans(plan)]
    if not problems:
        print(f"{len(plan)} generated files are up to date.")
        return 0
    print("Generated copies are stale. Run scripts/sync_skill_distribution.py:")
    for path in problems:
        print(f"  {path.relative_to(REPO_ROOT)}")
    return 1


def write() -> int:
    plan = _plan()
    for destination in _orphans(plan):
        destination.unlink()
        print(f"removed  {destination.relative_to(REPO_ROOT)}")
    written = 0
    for source, destination in plan:
        if not _drift(source, destination):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        print(f"wrote    {destination.relative_to(REPO_ROOT)}")
        written += 1
    print(f"{len(plan)} generated files, {written} rewritten.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero instead of writing",
    )
    arguments = parser.parse_args()
    return check() if arguments.check else write()


if __name__ == "__main__":
    sys.exit(main())
