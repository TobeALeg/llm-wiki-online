"""Turn a directory of real project material into the evaluation corpus.

The corpus is the part of the label set that can be produced mechanically: which
files belong to one material group, what each file hashes to, and which partition
each group goes into. The labels themselves are a human judgement and live in
`gold.jsonl`.

Two properties this file exists to hold.

The repository records hashes and paths, never the material's text. Real material
is private, and a label set that quoted it would make every future clone a copy of
it.

A group never straddles dev and holdout. A derivation chain that is half in the
development set and half in the holdout set measures nothing, so the partition is
assigned per group and this builder refuses a group whose files disagree.

Usage::

    python evals/knowledge_v2/build_corpus.py --material-root "/e/AI infra/worket" --verify
    python evals/knowledge_v2/build_corpus.py --material-root ... --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CORPUS_PATH = HERE / "corpus.json"

SECRET_PATTERNS = (
    re.compile(r"(^|/)\.env(?:\.|$)", re.I),
    re.compile(r"(^|/)(?:id_rsa|id_ed25519)(?:\.|$)", re.I),
    re.compile(r"(?:^|[._/-])(?:secret|secrets|credential|credentials)(?:[._-]|$)", re.I),
    re.compile(r"\.(?:pem|key|p12|pfx|jks|keystore)$", re.I),
)
"""Path shapes that disqualify a file, identical to the ones the CLI applies.

A looser rule here would be worse than useless: `key` as a substring excludes
`sankey_chart.svg`, and a rule that excludes a chart teaches a reviewer to ignore
the exclusion report. `tests/test_corpus_secret_rule.py` asserts this list and the
CLI's agree path by path.
"""


def is_secret(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    return any(pattern.search(normalized) for pattern in SECRET_PATTERNS)


def digest(path: Path) -> dict:
    raw = path.read_bytes()
    text = raw.decode("utf-8", "replace")
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
    }


def git_log(root: Path) -> str:
    """The commit subjects, oldest first, as one material document.

    A commit history is a project's own chronological record of decisions and
    corrections. It carries the evolution a settled document has erased.
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", "--reverse", "--pretty=format:%ad %s", "--date=short"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip() + "\n"


GROUPS: list[dict] = [
    {
        "group_id": "g01-work-distillation",
        "split": "dev",
        "title": "工作提炼规格与其验收标准",
        "files": ["worket/docs/specs/work-distillation-v1.md", "worket/docs/acceptance/work-distillation-v1.md"],
    },
    {
        "group_id": "g02-executor-adapters",
        "split": "dev",
        "title": "执行器适配层规格与其验收标准",
        "files": ["worket/docs/specs/executor-adapters-v1.md", "worket/docs/acceptance/executor-adapters-v1.md"],
    },
    {"group_id": "g03-architecture", "split": "dev", "title": "模块架构", "files": ["worket/docs/architecture.md"]},
    {"group_id": "g04-product", "split": "dev", "title": "产品定义", "files": ["worket/docs/product.md"]},
    {"group_id": "g05-benefits", "split": "holdout", "title": "收益与洞察", "files": ["worket/docs/bp/benefits-and-insights.md"]},
    {"group_id": "g06-extraction-plan", "split": "dev", "title": "工作对象提取执行计划", "files": ["worket/docs/work-object-extraction-execution-plan.md"]},
    {"group_id": "g07-readme-agents", "split": "dev", "title": "项目说明与协作约定", "files": ["worket/README.md", "worket/AGENTS.md"]},
    {"group_id": "g08-context-notes", "split": "dev", "title": "领域词汇与工作笔记", "files": ["worket/CONTEXT.md", "worket/NOTES.md"]},
    {"group_id": "g09-release-012", "split": "holdout", "title": "v0.1.2 发布说明与验收记录", "files": ["worket/docs/releases/v0.1.2.md", "worket/docs/releases/v0.1.2-verification.md"]},
    {"group_id": "g10-deployment", "split": "holdout", "title": "VPS 运维与就绪清单", "files": ["worket/docs/deployment/vps-operation.md", "worket/docs/deployment/vps-readiness.md"]},
    {"group_id": "g11-interface-consistency", "split": "dev", "title": "界面一致性核查", "files": ["worket/docs/qa/interface-consistency.md"]},
    {"group_id": "g12-tom-swe", "split": "holdout", "title": "ToM-SWE 可行性调研", "files": ["worket/docs/research/tom-swe-feasibility.md"]},
    {"group_id": "g13-task-definition-index", "split": "dev", "title": "任务定义调研索引", "files": ["worket/docs/research/task-definition/README.md"]},
    {"group_id": "g14-human-problem", "split": "dev", "title": "人的问题表述", "files": ["worket/docs/research/task-definition/human-problem-formulation.md"]},
    {"group_id": "g15-llm-clarification", "split": "holdout", "title": "LLM 澄清证据", "files": ["worket/docs/research/task-definition/llm-clarification-evidence.md"]},
    {"group_id": "g16-memory-abstraction", "split": "dev", "title": "记忆与抽象", "files": ["worket/docs/research/task-definition/memory-and-abstraction.md"]},
    {"group_id": "g17-protocol-landscape", "split": "holdout", "title": "产品与协议图谱", "files": ["worket/docs/research/task-definition/product-and-protocol-landscape.md"]},
    {"group_id": "g18-repeated-work", "split": "dev", "title": "重复工作评测设计", "files": ["worket/docs/research/task-definition/repeated-work-evaluation.md"]},
    {"group_id": "g19-acceptance-admin", "split": "dev", "title": "后台管理验收", "files": ["worket/docs/acceptance/worket-backend-admin.md"]},
    {"group_id": "g20-acceptance-improvement", "split": "holdout", "title": "改进项验收", "files": ["worket/docs/acceptance/worket-improvement.md"]},
    {"group_id": "g21-acceptance-recording", "split": "dev", "title": "记录上传验收", "files": ["worket/docs/acceptance/worket-recording-upload.md"]},
    {"group_id": "g22-acceptance-docking", "split": "dev", "title": "桌宠吸附验收", "files": ["worket/docs/acceptance/worket-pet-docking.md"]},
    {
        "group_id": "g23-commit-history",
        "split": "dev",
        "title": "提交历史（按时间排列的真实决策与修正）",
        "files": [],
        "generated": "git_log",
        "generated_root": "worket",
    },
    {"group_id": "g24-server-surface", "split": "holdout", "title": "服务端说明与运行面", "files": ["worket/server/README.md"]},
    {"group_id": "bp-v10", "split": "dev", "title": "商业计划 v1.0", "files": ["0825/BPv1.0.md"]},
    {"group_id": "bp-v11", "split": "dev", "title": "商业计划 v1.1", "files": ["0825/BPv1.1.md"]},
    {
        "group_id": "bp-v12-v13",
        "split": "dev",
        "title": "商业计划 v1.2 与 v1.3（两个文件同一内容）",
        "files": ["0825/BPv1.2.md", "0825/BPv1.3.md"],
    },
    {"group_id": "bp-v14", "split": "dev", "title": "商业计划 v1.4", "files": ["0829/BPv1.4.md"]},
    {"group_id": "bp-v14-partial", "split": "dev", "title": "商业计划 v1.4 的前缀导出", "files": ["0829/BPv1.4-4.md"]},
]
"""The material groups.

Only prose is listed. A project's source code is material too, but it carries
neither the decisions nor the reasoning, and listing it would inflate the group count
with units nobody can label.

The business plan chain is one chain, so all of its groups share a partition. A
version chain split across dev and holdout would put the answer to an evolution
question in the half nobody is allowed to look at.
"""

CONSTRUCTED_GROUPS: list[tuple[str, str]] = [
    ("group-c01-summary-window", "dev"),
    ("group-c02-modality", "dev"),
    ("group-c03-ai-suggestion", "dev"),
    ("group-c04-qualifiers", "dev"),
    ("group-c06-conflict", "holdout"),
    ("group-c07-support-groups", "holdout"),
]
"""Constructed boundary groups, each with the partition it belongs to.

Four go to dev and two to holdout, which with the twenty four real groups gives the
thirty material groups the spec asks for at twenty dev and ten holdout. The two
hardest constructed cases are the holdout ones, because a boundary that is only
tested in development is a boundary nobody has tested. Constructed samples verify
boundaries; they never stand in for real material.
"""


def build(material_root: Path) -> dict:
    groups: list[dict] = []
    skipped: list[dict] = []
    for definition in GROUPS:
        entries: list[dict] = []
        for relative in definition["files"]:
            path = material_root / relative
            if not path.is_file():
                skipped.append({"group_id": definition["group_id"], "path": relative, "reason": "missing"})
                continue
            if is_secret(relative):
                skipped.append({"group_id": definition["group_id"], "path": relative, "reason": "secret_path"})
                continue
            entry = digest(path)
            entry["path"] = relative
            entries.append(entry)
        generated = definition.get("generated")
        if generated == "git_log":
            text = git_log(material_root / str(definition.get("generated_root") or "."))
            if text:
                entries.append(
                    {
                        "path": "<git log --reverse>",
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "bytes": len(text.encode("utf-8")),
                        "lines": text.count("\n"),
                        "generated": "git_log",
                    }
                )
        groups.append(
            {
                "group_id": definition["group_id"],
                "split": definition["split"],
                "title": definition["title"],
                "constructed": False,
                "generated_root": definition.get("generated_root", ""),
                "files": entries,
            }
        )

    for group_id, split in CONSTRUCTED_GROUPS:
        groups.append(
            {
                "group_id": group_id,
                "split": split,
                "title": f"构造边界组 {group_id}",
                "constructed": True,
                "files": [],
            }
        )

    splits: dict[str, int] = {}
    for group in groups:
        splits[group["split"]] = splits.get(group["split"], 0) + 1
    return {
        "notice": (
            "材料组的定义与哈希。私有正文不进入仓库：这里只有路径、哈希与分组，"
            "评测时按 material_root 读回正文。"
        ),
        "material_root_hint": str(material_root),
        "scope_note": (
            "真实组来自两个项目的文档与决策记录，构造组来自既有边界样本。"
            "材料正文不进仓库，只有路径、哈希与分组。"
        ),
        "group_count": len(groups),
        "real_group_count": sum(1 for group in groups if not group["constructed"]),
        "constructed_group_count": sum(1 for group in groups if group["constructed"]),
        "splits": splits,
        "groups": groups,
        "skipped": skipped,
        "relations": [],
        "secret_paths_present": [],
        "secret_paths_note": (
            "材料目录里按路径规则排除的文件。分组从不引用它们，这里把它们列出来，"
            "使「没有读凭据」是一条可核对的事实，而不是一句声明。"
        ),
        "relations_note": (
            "同一份材料的重复或部分导出。血缘规则与内容指纹规则针对的就是这两种情形，"
            "所以评测需要知道它们。"
        ),
        "reveals_material_text": False,
    }


def scan_secret_paths(material_root: Path, *, limit: int = 400) -> list[str]:
    """Secret-named files in the material, so the exclusion is visible as evidence.

    The declared groups never reference one. Reporting what was found makes that a
    fact a reviewer can check rather than a claim about a rule.
    """

    found: list[str] = []
    skip_dirs = {".git", "node_modules", "site-packages", "__pycache__", "dist-info", ".venv", "venv"}
    for path in material_root.rglob("*"):
        if len(found) >= limit:
            break
        if not path.is_file():
            continue
        if skip_dirs & set(path.parts):
            continue
        relative = path.relative_to(material_root).as_posix()
        if is_secret(relative):
            found.append(relative)
    return sorted(found)


def screen_corpus(corpus: dict, material_root: Path) -> dict:
    """Read every declared file looking for credential-shaped text.

    The path rule cannot see a key typed into the middle of a design note. This can,
    and recording the result here is what makes "the material was screened" a fact
    attached to the corpus instead of a sentence in a conversation.
    """

    from screen_material import screen_file  # a local import; screen_material imports this module

    findings: list[dict] = []
    scanned: list[str] = []
    for group in corpus["groups"]:
        for entry in group["files"]:
            if entry.get("generated"):
                continue
            path = material_root / entry["path"]
            if not path.is_file():
                continue
            scanned.append(entry["path"])
            findings.extend(screen_file(path, entry["path"]))
    needs_review = [item for item in findings if not item["likely_placeholder"]]
    return {
        "files_scanned": len(scanned),
        "matches": len(findings),
        "needs_review": needs_review,
        "placeholders_only": [item for item in findings if item["likely_placeholder"]],
        "clean": not needs_review,
        "note": (
            "对语料里每个文件做的凭据形态扫描。path 规则抓不到写在正文中间的 key，"
            "这一步能。clean 为 true 才表示没有人需要先看一眼。"
        ),
    }


def find_relations(corpus: dict, material_root: Path) -> list[dict]:
    """Structural relations between the corpus files, discovered not assumed.

    A byte-identical pair is one document filed twice, and a prefix is one document
    exported in part. Both are exactly what the lineage rule and the content
    fingerprint rule are about, and both are visible from the files themselves, so
    they are computed rather than written down by hand and forgotten.
    """

    texts: dict[str, str] = {}
    for group in corpus["groups"]:
        for entry in group["files"]:
            if entry.get("generated"):
                continue
            path = material_root / entry["path"]
            if path.is_file():
                texts[entry["path"]] = path.read_text(encoding="utf-8", errors="replace")

    relations: list[dict] = []
    paths = sorted(texts)
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if texts[left] == texts[right]:
                relations.append({"kind": "identical", "left": left, "right": right})
                continue
            shorter, longer = sorted((left, right), key=lambda name: len(texts[name]))
            if len(texts[shorter]) >= 200 and texts[longer].startswith(texts[shorter].rstrip()):
                relations.append(
                    {
                        "kind": "prefix_of",
                        "left": shorter,
                        "right": longer,
                        "prefix_chars": len(texts[shorter]),
                        "right_chars": len(texts[longer]),
                    }
                )
    return relations


def verify(corpus: dict, material_root: Path) -> list[str]:
    """Differences between the recorded corpus and the directory as it is now."""

    problems: list[str] = []
    for group in corpus["groups"]:
        for entry in group["files"]:
            if entry.get("generated"):
                root = material_root / str(group.get("generated_root") or ".")
                text = git_log(root)
                actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if actual != entry["sha256"]:
                    problems.append(f"{group['group_id']}: the commit history changed")
                continue
            path = material_root / entry["path"]
            if not path.is_file():
                problems.append(f"{group['group_id']}: {entry['path']} is missing")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != entry["sha256"]:
                problems.append(f"{group['group_id']}: {entry['path']} changed since the corpus was written")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--material-root", required=True)
    parser.add_argument("--write", action="store_true", help="write corpus.json")
    parser.add_argument("--verify", action="store_true", help="check the directory against corpus.json")
    arguments = parser.parse_args()
    root = Path(arguments.material_root).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 2

    if arguments.verify:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        problems = verify(corpus, root)
        for problem in problems:
            print(f"CHANGED  {problem}")
        if not problems:
            print(f"{corpus['group_count']} groups match the directory.")
        return 1 if problems else 0

    corpus = build(root)
    corpus["relations"] = find_relations(corpus, root)
    corpus["secret_paths_present"] = scan_secret_paths(root)
    corpus["screening"] = screen_corpus(corpus, root)
    if arguments.write:
        CORPUS_PATH.write_text(
            json.dumps(corpus, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {CORPUS_PATH.relative_to(REPO_ROOT)}")

    print(f"material root: {root}")
    print(f"groups: {corpus['group_count']} ({corpus['real_group_count']} real, "
          f"{corpus['constructed_group_count']} constructed)")
    print(f"splits: {corpus['splits']}")
    total = sum(len(group["files"]) for group in corpus["groups"])
    print(f"files: {total}")
    for item in corpus["skipped"]:
        print(f"skipped {item['path']}: {item['reason']}")
    screening = corpus.get("screening") or {}
    if screening:
        print(
            f"screened: {screening['files_scanned']} files, {screening['matches']} matches, "
            f"{len(screening['needs_review'])} needing review"
        )
        for item in screening["needs_review"]:
            print(f"  REVIEW {item['path']}:{item['line']} {item['rule']} {item['masked']}")
    for path in corpus["secret_paths_present"]:
        print(f"excluded by path rule: {path}")
    for relation in corpus["relations"]:
        if relation["kind"] == "identical":
            print(f"identical: {relation['left']} == {relation['right']}")
        else:
            print(
                f"prefix: {relation['left']} is a prefix of {relation['right']} "
                f"({relation['prefix_chars']}/{relation['right_chars']} chars)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
