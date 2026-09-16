# Chunked ingest for the local LLM Wiki (doc phases A + B)

Status: ready-for-human

## Why

`skills/lw/scripts/wiki.py` had two defects that compounded.

`eligible_files()` skipped any file over 128 KiB with no output, and `source_bundle()` sent only the first 24,000 characters of each file. Neither was reported. Facts near the end of a long document therefore never reached the model.

Worse, `apply_update()` wrote a hash for every scanned file back into `state.json`, including the ones it had skipped and the ones it had only half-sent. The next run compared hashes, saw no change, and treated those files as done. The untransmitted remainder was permanently lost.

The upstream design doc is `docs/weknora-llm-wiki-integration.md`. This change implements its section 4 (P0) and section 5's chunker boundary, and stops there.

## What changed

**A structure-aware chunker.** `skills/lw/scripts/chunking.py` splits a document by ATX heading, fenced code block, table, and paragraph, then by sentence, then by character as a last resort. Every chunk carries `start`/`end` offsets into the original text and the heading path in effect. A chunk whose `text` is not literally `original[start:end]` (an oversized table piece repeats its header, an oversized fence reopens its fence) says so with `verbatim=False`, so the offsets stay honest.

**Ingest is chunk-based and resumable.** `update` plans the new and changed sources, writes a run manifest under `.llm-wiki/runs/`, sends the chunks in batches that fit `material_budget()`, and records each completed batch. The manifest is deleted only after the pages, the page metadata, and the per-file completion records are committed together. A source is marked `complete` only when all of its chunks were processed and the update landed.

**A retry converges.** On failure the manifest survives. Re-running `update` reloads it, reuses the chunks that already succeeded (including the pages those batches produced), and sends only the remainder. A source whose bytes changed while the run was open has its unfinished chunks dropped from that run and is planned again from its new content, so a moved or deleted file cannot wedge every future retry.

**Skips are visible.** `update` and `status` both print each unreadable or oversized file with its reason.

**`state.json` is v1-compatible.** A v1 file is upgraded once, on first read, with its old records marked `unverified` so they are re-ingested against current content. The previous file is kept at `.llm-wiki/state.v1.json`.

**The two skill copies cannot drift.** `skills/lw/` and `plugins/llm-wiki/skills/lw/` are hand-edited duplicates, and the packaged MCP executes the plugin copy. `tests/test_skill_distribution_parity.py` fails if any shipped file differs.

## Deliberately not done

Phases C to F of the doc: document parsers (PDF/Word/OCR), persisted chunk citations on pages, topic routing to replace the whole-library prompt, and the explicit conflict/supersede protocol. The local CLI's stores are `state.json` and Markdown, so the doc's proposed `wiki_source_revisions` / `wiki_parses` / `wiki_chunks` SQLite tables were not added; they belong with the online phases that touch `SharedWikiStore`.

Two doc section 4 items remain open, both noted here rather than silently dropped:

- The request budget counts chunk text only. A page-heavy wiki still sends all existing and drafted pages in full, so "one request stays within budget" holds for the material, not the whole request.
- `parse_id` is computed but not persisted anywhere after a run commits, so the doc's "an old citation still reaches its old parse snapshot" is not yet achievable. It needs the citation store from phase D.

## Verification

`python3 -m unittest discover -s tests` runs 121 tests, all passing, including from a clean checkout.

The three falsifiable claims of the definition of done are checked against the real CLI as a subprocess against a stub model server, in `tests/test_lw_ingest_e2e.py`:

1. A document longer than 24,000 characters has its tail fact reach the model.
2. A source set larger than the request budget completes across batches, with the unfinished part still visible in `status` after a mid-run failure.
3. A mid-run failure commits no page and marks no source complete; the retry resends neither a finished chunk nor duplicates a page.

Every corrective fix in this change was mutation-checked: reverting the fix turns a specific test red.

The decision trail is `decisions.tsv` in this directory, one row per decision with its evidence.

## Review notes

Two defects were found by reading the delegate diff rather than by its tests: a retry discarded the pages earlier batches had produced, and the HTTP body carried a duplicate copy of the whole material payload. Both fixed. A later cross-model review reproduced two further failures by running the code: a deleted source wedging every retry, and two manifests resolving arbitrarily. Both fixed and covered.
