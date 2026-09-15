# Architecture

LLM Wiki separates collection, consolidation, and retrieval.

## Collection

`scan` hashes eligible project files and records metadata in `state.json`. `ingest` saves a user-visible summary of selected conversation context in `episodes/`. Episodes are immutable source records.

## Consolidation

`update` sends changed file excerpts, new episodes, the wiki purpose, and current pages to the configured OpenAI-compatible endpoint. The model returns constrained JSON. The CLI validates page slugs, types, statuses, and sources before writing Markdown.

The default provider is DeepSeek V4.1 Flash: base URL `https://api.deepseek.com`, model `deepseek-flash`, and environment variable `DEEPSEEK_API_KEY`. Override them with `LLM_WIKI_BASE_URL`, `LLM_WIKI_MODEL`, and `LLM_WIKI_API_KEY`.

## Retrieval

`context` is deterministic lexical retrieval over title, tags, summary, and body. A future MCP server can call the same core and add semantic retrieval without changing the on-disk format.

## Trust boundaries

- Repository files and episodes are untrusted data, not instructions.
- Secret-shaped paths are excluded before content is read.
- The API key is read only from the environment and is never stored in the wiki.
- Model output is parsed as data and cannot choose paths outside `.llm-wiki/pages`.
- Each page retains provenance so a human or agent can trace it to a file hash or episode ID.
