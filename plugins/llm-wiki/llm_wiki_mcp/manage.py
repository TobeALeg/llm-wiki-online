"""Local-only project allowlist management."""

from __future__ import annotations

import argparse
import json

from .service import ServiceError, list_projects, register_project, run_wiki, unregister_project


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage projects exposed by LLM Wiki MCP.")
    sub = result.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="allow the MCP server to access a project")
    add.add_argument("project_id")
    add.add_argument("path")
    add.add_argument("--name")
    add.add_argument("--init", action="store_true", help="initialize .llm-wiki immediately")
    remove = sub.add_parser("remove", help="remove access without deleting wiki data")
    remove.add_argument("project_id")
    sub.add_parser("list", help="list allowed projects")
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "add":
            value = register_project(args.project_id, args.path, args.name)
            if args.init:
                run_wiki(value["id"], "init")
        elif args.command == "remove":
            value = unregister_project(args.project_id)
        else:
            value = list_projects()
    except ServiceError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
