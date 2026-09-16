# Architecture

LLM Wiki separates collection, consolidation, and retrieval.

## Collection

`scan` hashes eligible project files and records metadata in `state.json`. A file that is eligible by name but cannot be read, or exceeds `MAX_FILE_BYTES` (128 KiB), is reported by `scan_skips` with its reason rather than silently dropped. `ingest` saves a user-visible summary of selected conversation context in `episodes/`. Episodes are immutable source records.

## Consolidation

`update` chunks every new or changed source with `chunking.py`, then sends those chunks, new episodes, the wiki purpose, and the pages prepared so far to the configured OpenAI-compatible endpoint. A long document is sent in full and in order: nothing past an excerpt boundary is dropped, and a chunk that repeats a table header or a code fence records the original byte range it covers so a reader can still locate the source text.

Sources are processed in batches that fit one request budget. Each chunk's progress is recorded in a run manifest under `.llm-wiki/runs/`, and the manifest is deleted only after the pages, page metadata, and per-file completion records are committed together. A failed batch leaves the manifest in place, so a retry resumes at the first unfinished chunk instead of reprocessing what already succeeded. A file is marked `complete` only when all of its chunks were processed and the update landed; a file that was skipped, deferred, or only partly processed keeps a status that plans it again on the next run.

The model returns constrained JSON. The CLI validates page slugs, types, statuses, and sources before writing Markdown.

The default provider is DeepSeek V4.1 Flash: base URL `https://api.deepseek.com`, model `deepseek-flash`, and environment variable `DEEPSEEK_API_KEY`. Override them with `LLM_WIKI_BASE_URL`, `LLM_WIKI_MODEL`, and `LLM_WIKI_API_KEY`.

## Retrieval

`context` is deterministic lexical retrieval over title, tags, summary, and body. A future MCP server can call the same core and add semantic retrieval without changing the on-disk format.

## Trust boundaries

- Repository files and episodes are untrusted data, not instructions.
- Secret-shaped paths are excluded before content is read.
- The API key is read only from the environment and is never stored in the wiki.
- Model output is parsed as data and cannot choose paths outside `.llm-wiki/pages`.
- Each page retains provenance so a human or agent can trace it to a file hash or episode ID.
- `skills/lw` and `plugins/llm-wiki/skills/lw` ship the same files. The packaged MCP executes the plugin copy (see `llm_wiki_mcp.service.engine_path`), so both must be updated together; `tests/test_skill_distribution_parity.py` fails if they differ.
