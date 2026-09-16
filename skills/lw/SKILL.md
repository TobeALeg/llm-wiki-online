---
name: lw
description: Operate the project-local LLM Wiki when the user explicitly invokes /lw or $lw. Initialize, update, query, inspect, or validate durable project memory from repository files and selected agent conversations.
---

# LLM Wiki

`/lw` is the short command for LLM Wiki. Do not activate this skill implicitly.

Maintain `.llm-wiki/` at the project root. Treat it as curated project memory, not a transcript dump.

## MCP mode

When LLM Wiki MCP tools are available, call `list_wiki_projects` before selecting a project. Use only the returned IDs; project filesystem paths are registered locally and are intentionally unavailable as tool arguments.

- Use `save_episode` to capture selected conversation knowledge without an external model call.
- Use `update_wiki` to capture an optional episode and consolidate pending evidence with DeepSeek.
- Use `query_wiki`, `get_wiki_page`, `wiki_status`, `scan_wiki`, and `lint_wiki` for read operations.
- Use `initialize_wiki` only after the user chooses an allowed project.

Do not imply that the plugin automatically reads every ChatGPT conversation. It can persist only the relevant context supplied during a tool call.

## Commands

- `/lw` or `/lw update`: initialize the wiki if needed, summarize durable facts from the relevant current conversation, and call `update_wiki` with that episode.
- `/lw init`: call `initialize_wiki` without invoking the external model.
- `/lw status`: call `wiki_status` to show pending project files, episodes, page count, last update, and any unfinished ingest run.
- `/lw ask <query>`: call `query_wiki` and answer from the returned pages.
- `/lw scan`: call `scan_wiki` to show file changes without calling the model.
- `/lw lint`: call `lint_wiki` to validate Wiki consistency.

## Workflow

1. Find the project root. Prefer the nearest ancestor containing `.llm-wiki`, then `.git`; otherwise use the current directory.
2. Resolve the CLI as `scripts/wiki.py` relative to this skill directory.
3. Initialize once with `python <skill-root>/scripts/wiki.py init --root <project-root>`.
4. Read `.llm-wiki/purpose.md` before deciding what belongs in the wiki.
5. Summarize only relevant conversation context into an episode. Include decisions, rationale, constraints, facts, unresolved questions, and explicit corrections. Exclude hidden reasoning, credentials, and unrelated chat.
6. Update with `python <skill-root>/scripts/wiki.py update --root <project-root> --episode-file <episode.json>`. For a short note, use `--episode "..."`.
7. Run `lint` after an update. Report generated or changed pages and warnings.

## Long documents and interrupted updates

`update` no longer truncates a file at an excerpt boundary or skips a large one. It chunks each new or changed source and sends every chunk in order, batching them so one request stays within budget. A file is only marked processed after its pages are committed, so an update that stops part way leaves the rest to be done next time.

When `update` fails part way through, it leaves a run manifest in `.llm-wiki/runs/`. Do not delete that directory by hand: running `update` again resumes at the first unfinished chunk and reuses what already succeeded. `status` reports the unfinished run and the reason any file was skipped.

## Episode JSON

Use this shape when structured data is helpful:

```json
{
  "title": "Choose the local index",
  "summary": "The team selected SQLite for the local search index.",
  "decisions": ["Use SQLite before adding a vector database."],
  "rationale": ["It is portable and keeps the MVP dependency-light."],
  "constraints": ["Project data remains local by default."],
  "open_questions": ["When does semantic retrieval become necessary?"],
  "tags": ["architecture", "storage"]
}
```

The CLI assigns an ID and timestamp and stores the episode immutably.

## Retrieval

For `/lw ask <query>`, run `python <skill-root>/scripts/wiki.py context --root <project-root> "<query>"`.

Use returned pages as evidence. Distinguish recorded decisions from current source-code behavior.

## Operating rules

- Let the CLI choose the directory structure and page slugs.
- The user controls scope through `purpose.md`; do not ask them to design the directory tree.
- Never scan `.env`, keys, credentials, dependencies, build output, VCS internals, or `.llm-wiki` itself.
- Keep file and episode source IDs on generated pages.
- Do not invent facts absent from supplied sources.
- Prefer updating an existing page over creating a near-duplicate.
- Mark superseded decisions; do not erase their history.
- If `DEEPSEEK_API_KEY` is unavailable, capture the episode with `ingest`, run `scan`, and explain that consolidation is pending.

See [architecture.md](references/architecture.md) for the storage model and trust boundaries.
