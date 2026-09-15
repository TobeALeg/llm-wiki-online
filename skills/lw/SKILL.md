---
name: lw
description: Operate the project-local LLM Wiki when the user explicitly invokes /lw or $lw. Initialize, update, query, inspect, or validate durable project memory from repository files and selected agent conversations.
---

# LLM Wiki

`/lw` is the short command for LLM Wiki. Do not activate this skill implicitly.

Maintain `.llm-wiki/` at the project root. Treat it as curated project memory, not a transcript dump.

## Commands

- `/lw` or `/lw update`: initialize the wiki if needed, summarize durable facts from the relevant current conversation, and update the wiki.
- `/lw init`: initialize `.llm-wiki/` without calling the external model.
- `/lw status`: show pending project files, episodes, page count, and last update.
- `/lw ask <query>`: retrieve relevant wiki pages and answer from them.
- `/lw scan`: show file changes without calling the model.
- `/lw lint`: validate wiki consistency.

## Workflow

1. Find the project root. Prefer the nearest ancestor containing `.llm-wiki`, then `.git`; otherwise use the current directory.
2. Resolve the CLI as `scripts/wiki.py` relative to this skill directory.
3. Initialize once with `python <skill-root>/scripts/wiki.py init --root <project-root>`.
4. Read `.llm-wiki/purpose.md` before deciding what belongs in the wiki.
5. Summarize only relevant conversation context into an episode. Include decisions, rationale, constraints, facts, unresolved questions, and explicit corrections. Exclude hidden reasoning, credentials, and unrelated chat.
6. Update with `python <skill-root>/scripts/wiki.py update --root <project-root> --episode-file <episode.json>`. For a short note, use `--episode "..."`.
7. Run `lint` after an update. Report generated or changed pages and warnings.

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
