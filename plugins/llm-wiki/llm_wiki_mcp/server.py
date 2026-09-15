"""MCP tools for project-scoped LLM Wiki operations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .service import episode_json, list_projects, read_page, run_wiki
from .auth import AuthService, AuthStore
from .remote_mcp import create_remote_mcp
from .remote_service import RemoteWikiService
from .shared_service import SharedWikiService
from .store import SharedWikiStore

INSTRUCTIONS = (
    "Call list_projects before using a project ID. Only save durable facts selected from the "
    "current conversation; never save secrets or hidden reasoning. Use save_episode to capture "
    "facts without an LLM call, and update_wiki to consolidate pending evidence with DeepSeek."
)

mcp = FastMCP(
    "llm-wiki",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
)


def database_path() -> Path:
    configured = os.environ.get("LLM_WIKI_DATABASE", "data/lw.sqlite3")
    return Path(configured).expanduser().resolve()


def create_company_mcp() -> FastMCP:
    database = database_path()
    auth = AuthService(AuthStore(database))
    shared = SharedWikiService(SharedWikiStore(database))
    local = RemoteWikiService()
    return create_remote_mcp(
        auth,
        shared.status,
        read_search=shared.search,
        read_page=shared.page,
        read_versions=shared.versions,
        submit_update=shared.submit,
        restore_page=shared.restore,
        organize_local=local.organize_local,
    )

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


@mcp.tool(
    title="List allowed wiki projects",
    description="List the project IDs that this local MCP server is explicitly allowed to access.",
    annotations=READ_ONLY,
)
def list_wiki_projects() -> dict[str, Any]:
    projects = list_projects()
    return {"projects": projects, "count": len(projects)}


@mcp.tool(
    title="Initialize a project wiki",
    description="Create the .llm-wiki structure for an already allowed project without calling an LLM.",
    annotations=WRITE,
)
def initialize_wiki(project_id: str) -> dict[str, Any]:
    return run_wiki(project_id, "init", timeout=30)


@mcp.tool(
    title="Save a conversation episode",
    description="Save selected durable facts from the current conversation without calling an external LLM.",
    annotations=WRITE,
)
def save_episode(
    project_id: str,
    summary: str,
    title: str = "Agent conversation",
    decisions: list[str] | None = None,
    rationale: list[str] | None = None,
    constraints: list[str] | None = None,
    open_questions: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    episode = episode_json(
        title, summary, decisions, rationale, constraints, open_questions, tags
    )
    return run_wiki(project_id, "ingest", ["--episode", episode], timeout=30)


@mcp.tool(
    title="Update a project wiki",
    description="Consolidate pending project changes and episodes into wiki pages with the configured DeepSeek model.",
    annotations=WRITE,
)
def update_wiki(
    project_id: str,
    summary: str | None = None,
    title: str = "Agent conversation",
    decisions: list[str] | None = None,
    rationale: list[str] | None = None,
    constraints: list[str] | None = None,
    open_questions: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    arguments: list[str] = []
    if summary is not None:
        arguments = [
            "--episode",
            episode_json(title, summary, decisions, rationale, constraints, open_questions, tags),
        ]
    return run_wiki(project_id, "update", arguments, timeout=300)


@mcp.tool(
    title="Search a project wiki",
    description="Retrieve the most relevant existing wiki pages for a project-history question.",
    annotations=READ_ONLY,
)
def query_wiki(project_id: str, query: str, limit: int = 5) -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 20))
    return run_wiki(
        project_id, "context", [query, "--limit", str(bounded_limit)], timeout=30
    )


@mcp.tool(
    title="Get wiki status",
    description="Show pending files, pending episodes, page count, and last update for an allowed project.",
    annotations=READ_ONLY,
)
def wiki_status(project_id: str) -> dict[str, Any]:
    return run_wiki(project_id, "status", timeout=30)


@mcp.tool(
    title="Scan project changes",
    description="List project files added, changed, or removed since the last consolidated wiki update.",
    annotations=READ_ONLY,
)
def scan_wiki(project_id: str) -> dict[str, Any]:
    return run_wiki(project_id, "scan", timeout=30)


@mcp.tool(
    title="Read a wiki page",
    description="Read one wiki page by the stable slug returned by search or status workflows.",
    annotations=READ_ONLY,
)
def get_wiki_page(project_id: str, slug: str) -> dict[str, Any]:
    return read_page(project_id, slug)


@mcp.tool(
    title="Validate a project wiki",
    description="Check the wiki catalog, page files, front matter, and index for consistency.",
    annotations=READ_ONLY,
)
def lint_wiki(project_id: str) -> dict[str, Any]:
    return run_wiki(project_id, "lint", timeout=30)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the LLM Wiki MCP server.")
    result.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", default=4310, type=int)
    return result


def main() -> None:
    args = parser().parse_args()
    selected = create_company_mcp() if os.environ.get("LLM_WIKI_REMOTE", "").lower() in {"1", "true", "yes"} else mcp
    selected.settings.host = args.host
    selected.settings.port = args.port
    selected.run(transport=args.transport)


if __name__ == "__main__":
    main()
