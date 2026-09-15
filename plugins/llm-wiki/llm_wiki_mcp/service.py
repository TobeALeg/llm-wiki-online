"""Project registry and safe adapter around the existing LLM Wiki CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

REGISTRY_VERSION = 1
PROJECT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
PAGE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_EPISODE_CHARS = 100_000
MAX_TOOL_OUTPUT_CHARS = 200_000


class ServiceError(RuntimeError):
    """A safe, user-readable service failure."""


def registry_path() -> Path:
    configured = os.environ.get("LLM_WIKI_REGISTRY")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".llm-wiki" / "projects.json"


def engine_path() -> Path:
    configured = os.environ.get("LLM_WIKI_ENGINE")
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        plugin_root = Path(__file__).resolve().parents[1]
        path = plugin_root / "skills" / "lw" / "scripts" / "wiki.py"
    if not path.is_file():
        raise ServiceError(f"LLM Wiki engine was not found at {path}")
    return path


def _empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "projects": {}}


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return _empty_registry()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"Cannot read project registry {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != REGISTRY_VERSION:
        raise ServiceError(f"Unsupported project registry at {path}")
    if not isinstance(value.get("projects"), dict):
        raise ServiceError(f"Malformed project registry at {path}")
    return value


def save_registry(value: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    if os.name != "nt":
        path.chmod(0o600)


def register_project(project_id: str, project_path: str, name: str | None = None) -> dict[str, Any]:
    normalized_id = project_id.strip().lower()
    if not PROJECT_ID.fullmatch(normalized_id):
        raise ServiceError(
            "Project ID must be 1-64 lowercase letters, digits, underscores, or hyphens."
        )
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        raise ServiceError(f"Project directory does not exist: {root}")
    registry = load_registry()
    registry["projects"][normalized_id] = {
        "name": (name or root.name or normalized_id).strip(),
        "path": str(root),
    }
    save_registry(registry)
    return public_project(normalized_id, registry["projects"][normalized_id])


def unregister_project(project_id: str) -> dict[str, Any]:
    registry = load_registry()
    if project_id not in registry["projects"]:
        raise ServiceError(f"Unknown project ID: {project_id}")
    record = registry["projects"].pop(project_id)
    save_registry(registry)
    return public_project(project_id, record)


def public_project(project_id: str, record: dict[str, Any]) -> dict[str, Any]:
    root = Path(record["path"])
    return {
        "id": project_id,
        "name": record.get("name") or project_id,
        "available": root.is_dir(),
        "wiki_initialized": (root / ".llm-wiki" / "state.json").is_file(),
    }


def list_projects() -> list[dict[str, Any]]:
    registry = load_registry()
    return [
        public_project(project_id, record)
        for project_id, record in sorted(registry["projects"].items())
    ]


def project_root(project_id: str) -> Path:
    registry = load_registry()
    record = registry["projects"].get(project_id)
    if not isinstance(record, dict):
        raise ServiceError(
            f"Unknown project ID: {project_id}. Register it locally with llm-wiki-projects."
        )
    root = Path(str(record.get("path", ""))).resolve()
    if not root.is_dir():
        raise ServiceError(f"Registered project is unavailable: {project_id}")
    return root


def run_wiki(
    project_id: str,
    command: str,
    arguments: Sequence[str] = (),
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    root = project_root(project_id)
    process = subprocess.run(
        [sys.executable, str(engine_path()), command, "--root", str(root), *arguments],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )
    stdout = process.stdout.strip()
    stderr = process.stderr.strip()
    if len(stdout) > MAX_TOOL_OUTPUT_CHARS:
        stdout = stdout[:MAX_TOOL_OUTPUT_CHARS] + "\n[output truncated]"
    if process.returncode:
        detail = stderr or stdout or f"exit code {process.returncode}"
        raise ServiceError(f"LLM Wiki {command} failed: {detail}")
    return {
        "project_id": project_id,
        "command": command,
        "ok": True,
        "output": stdout,
    }


def episode_json(
    title: str,
    summary: str,
    decisions: list[str] | None = None,
    rationale: list[str] | None = None,
    constraints: list[str] | None = None,
    open_questions: list[str] | None = None,
    tags: list[str] | None = None,
) -> str:
    if not summary.strip():
        raise ServiceError("Episode summary cannot be empty.")
    payload = {
        "title": title.strip() or "Agent conversation",
        "summary": summary.strip(),
        "decisions": decisions or [],
        "rationale": rationale or [],
        "constraints": constraints or [],
        "open_questions": open_questions or [],
        "tags": tags or [],
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) > MAX_EPISODE_CHARS:
        raise ServiceError(f"Episode exceeds {MAX_EPISODE_CHARS} characters.")
    return encoded


def read_page(project_id: str, slug: str) -> dict[str, Any]:
    if not PAGE_SLUG.fullmatch(slug):
        raise ServiceError("Page slug must contain lowercase letters, digits, and hyphens only.")
    path = project_root(project_id) / ".llm-wiki" / "pages" / f"{slug}.md"
    if not path.is_file():
        raise ServiceError(f"Wiki page does not exist: {slug}")
    content = path.read_text(encoding="utf-8")
    if len(content) > MAX_TOOL_OUTPUT_CHARS:
        content = content[:MAX_TOOL_OUTPUT_CHARS] + "\n[page truncated]"
    return {"project_id": project_id, "slug": slug, "content": content}
