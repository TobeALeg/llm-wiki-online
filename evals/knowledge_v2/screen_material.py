"""Screen material for secrets before it is read into a knowledge system.

The path rule catches a file named `credentials.ts`. It cannot catch a key typed
into the middle of a design note, which is the commoner accident: a curl example, a
troubleshooting note, a config snippet pasted while explaining something.

So this reads the text and reports what looks like a credential, with the location
and a masked excerpt. It is a screen, not a gate: a match may be a placeholder, a
documented example, or a real key, and only a person can tell those apart. What it
removes is the excuse of not having looked.

Matching is reported, never repaired. Editing somebody's material to remove a line
would leave them believing the file is clean.

Usage::

    python evals/knowledge_v2/screen_material.py --root "/e/AI infra/worket/docs"
    python evals/knowledge_v2/screen_material.py --root ... --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_corpus import is_secret  # noqa: E402

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("provider key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("aws key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password assignment", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S{4,}")),
    ("secret assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|access[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("url with credentials", re.compile(r"://[^\s/:@]{1,64}:[^\s/@]{4,}@")),
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("cn mobile number", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("private ipv4", re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")),
    ("ssh target", re.compile(r"\b(?:ssh|scp)\s+[^\s]*@[^\s]+")),
)

PLACEHOLDER_HINTS = (
    "example",
    "placeholder",
    "your-",
    "yourkey",
    "xxx",
    "***",
    "redacted",
    "<",
    "示例",
    "占位",
)
"""Text near a match that usually means it is documentation rather than a key."""


def mask(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "…"
    return f"{value[:4]}…{value[-2:]}"


def screen_file(path: Path, relative: str) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return [{"path": relative, "rule": "unreadable", "line": 0, "detail": type(error).__name__}]
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in RULES:
            match = pattern.search(line)
            if not match:
                continue
            window = line[max(0, match.start() - 40) : match.end() + 40].lower()
            likely_placeholder = any(hint in window for hint in PLACEHOLDER_HINTS)
            findings.append(
                {
                    "path": relative,
                    "line": number,
                    "rule": rule,
                    "masked": mask(match.group(0)),
                    "likely_placeholder": likely_placeholder,
                    "context_hint": "near a placeholder word" if likely_placeholder else "no placeholder word nearby",
                }
            )
    return findings


def candidate_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    skip_dirs = {".git", "node_modules", "site-packages", "__pycache__", "dist-info", ".venv", "venv", "dist", "build"}
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if skip_dirs & set(path.parts):
            continue
        found.append(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--suffix", action="append", default=None, help="default .md and .txt")
    parser.add_argument("--json", help="write the findings here")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    arguments = parser.parse_args()

    root = Path(arguments.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 2
    suffixes = tuple(arguments.suffix or (".md", ".txt", ".markdown"))
    files = candidate_files(root, suffixes)

    excluded_by_path = [path for path in files if is_secret(path.relative_to(root).as_posix())]
    findings: list[dict] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if is_secret(relative):
            continue
        findings.extend(screen_file(path, relative))

    real = [item for item in findings if not item["likely_placeholder"]]
    if not arguments.quiet:
        for item in findings:
            flag = "probably a placeholder" if item["likely_placeholder"] else "REVIEW"
            print(f"{flag:<22} {item['path']}:{item['line']}  {item['rule']}  {item['masked']}")

    print()
    print(f"files scanned: {len(files)} (suffixes {' '.join(suffixes)})")
    print(f"excluded by path rule: {len(excluded_by_path)}")
    for path in excluded_by_path:
        print(f"  {path.relative_to(root).as_posix()}")
    print(f"matches: {len(findings)} ({len(real)} with no placeholder word nearby)")
    if real:
        print("These need a person to look before the material is ingested:")
        for item in real:
            print(f"  {item['path']}:{item['line']} {item['rule']} {item['masked']} ({item['context_hint']})")
    if arguments.json:
        Path(arguments.json).write_text(
            json.dumps({"root": str(root), "findings": findings, "needs_review": real}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {arguments.json}")
    return 1 if real else 0


if __name__ == "__main__":
    raise SystemExit(main())
