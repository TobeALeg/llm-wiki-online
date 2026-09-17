"""Retrieval over committed claims: search, answer context, why chains, quotes.

The knowledge layer answers from claims. Source text is reached only when a caller
asks for it, and every piece of it says where it came from: a quote comes back
through the frozen artifact and its recorded span hashes, and an on-demand text
search is marked `raw_derived` because nothing validated that text on the way in.

Chinese recall is a character n-gram problem, not a whitespace problem. A query
with no spaces is tokenised into its characters and the bigrams between them, so
"数据库选型" reaches the claim that says "选型时先验证数据库的写入延迟" without a
dictionary and without a model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .claim_store import ClaimStoreError
    from .evidence import EvidenceError, OFFSET_UNIT, line_spans
    from .knowledge_types import KnowledgeError, Scope
    from .projection import (
        RENDERER_VERSION,
        claim_version_row,
        labels_for_claim,
        scope_columns,
        stale_projection_fallback,
    )
except ImportError:  # pragma: no cover - the packaged layout
    from claim_store import ClaimStoreError
    from evidence import EvidenceError, OFFSET_UNIT, line_spans
    from knowledge_types import KnowledgeError, Scope
    from projection import (
        RENDERER_VERSION,
        claim_version_row,
        labels_for_claim,
        scope_columns,
        stale_projection_fallback,
    )


EMBEDDING_UNAVAILABLE = "embedding_channel_unavailable"
EMBEDDING_FAILED = "embedding_channel_failed"
PAGE_INDEX_UNAVAILABLE = "page_index_unavailable"
STALE_PROJECTION_PRESENT = "stale_projection_present"
RENDERER_OUTDATED = "projection_renderer_outdated"

MODE_WHY = "why"
MODE_HOW = "how"

STALE_PAGE_STATUS = "stale"
FALLBACK_CONTENT_SOURCE = "stale_projection_fallback"
STORED_CONTENT_SOURCE = "stored_page"

MAX_SOURCE_ITEMS = 5
"""On-demand source text is a fragment of the answer, never the archive."""

MAX_MATCHED_TEXT = 400

_TOKEN_RUN = re.compile(
    r"[0-9A-Za-z_]+"
    r"|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+"
)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")

_WHY_RELATION_TYPES = ("derived_from", "supports", "depends_on")
_HOW_RELATION_TYPES = ("depends_on",)
_ROLE_FOR_RELATION = {
    "derived_from": "premise",
    "supports": "support",
    "depends_on": "dependency",
    "refines": "refinement",
}


def tokenize(text: str) -> list[str]:
    """The one tokenisation this layer uses, for Chinese and for Latin alike.

    A Latin run becomes one lowercased word. A Chinese run becomes its characters
    plus the bigrams between them, because a Chinese sentence has no spaces to
    split on and a single character is too blunt a match on its own.
    """

    tokens: list[str] = []
    for run in _TOKEN_RUN.findall(str(text if text is not None else "")):
        if _CJK_RUN.fullmatch(run):
            tokens.extend(run)
            for index in range(len(run) - 1):
                tokens.append(run[index : index + 2])
        else:
            tokens.append(run.lower())
    return tokens


def rrf_rank(*rankings: Sequence[str], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion of several rankings, with no learned weights.

    One id in two rankings is one answer found twice, which is what the summed
    1 / (k + rank) terms score. Ties fall back to the id so the same rankings
    always produce the same order, and a test can assert the merge itself.
    """

    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, identifier in enumerate(ranking, start=1):
            key = str(identifier)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def search_knowledge(
    *,
    store: Any,
    scope: Scope,
    query: str,
    limit: int = 10,
    kinds: Sequence[str] | None = None,
    embedding: Any = None,
) -> dict[str, Any]:
    """Page search and claim search over one project, merged deterministically.

    The order is fixed: the store filters by scope and kind, both channels recall,
    results are deduplicated by claim id, and the merge is reciprocal rank fusion.
    A page and a claim that carry the same claim are one answer, not two pieces of
    evidence: the page keeps the claim and the separate claim entry is dropped, so
    the reader gets the page context with the claim's own status and citation.

    `embedding` is an optional callable from text to a vector. When it is absent
    the answer is lexical and says so; when it fails, the answer stays lexical and
    the failure is named instead of hidden behind an empty ranking.
    """

    snapshot_version = store.current_version(scope.project_id)
    counts = _counts(tokenize(query))
    if not counts:
        return {
            "results": [],
            "degraded": False,
            "degraded_reason": "",
            "snapshot_version": snapshot_version,
        }

    degraded: list[str] = []
    if embedding is None:
        degraded.append(EMBEDDING_UNAVAILABLE)
    kinds_filter = [str(kind) for kind in kinds] if kinds else None

    claims = [
        row
        for row in store.iter_claims(scope, knowledge_kinds=kinds_filter)
        if str(row.get("lifecycle_status") or "") != "retracted"
    ]
    claim_objects = {str(row["claim_id"]): row for row in claims}
    claim_scores: dict[str, float] = {}
    claim_matched: dict[str, list[str]] = {}
    for row in claims:
        score, matched = _match_score(counts, _claim_text(row))
        if score <= 0:
            continue
        claim_id = str(row["claim_id"])
        claim_scores[claim_id] = score
        claim_matched[claim_id] = matched

    page_rows = _page_rows(store, scope)
    page_objects: dict[str, dict[str, Any]] = {}
    page_scores: dict[str, float] = {}
    page_matched: dict[str, list[str]] = {}
    if page_rows is None:
        degraded.append(PAGE_INDEX_UNAVAILABLE)
    else:
        for row in page_rows:
            page = _effective_page(row=row, claims=claims, degraded=degraded)
            score, matched = _match_score(counts, page["markdown"], title=str(row.get("title") or ""))
            if score <= 0:
                continue
            matched_claim = _best_claim(counts, page["claims"])
            if kinds_filter and (
                matched_claim is None or str(matched_claim.get("knowledge_kind") or "") not in kinds_filter
            ):
                continue
            slug = str(row["slug"])
            page["matched_claim"] = matched_claim
            page_objects[slug] = page
            page_scores[slug] = score
            page_matched[slug] = matched

    rankings = [
        [f"page:{slug}" for slug in _ranked(page_scores)],
        [f"claim:{claim_id}" for claim_id in _ranked(claim_scores)],
    ]
    if embedding is not None:
        semantic = _embedding_rankings(
            embedding=embedding,
            query=query,
            pages={f"page:{slug}": page_objects[slug]["markdown"] for slug in page_objects},
            claims={f"claim:{claim_id}": _claim_text(claim_objects[claim_id]) for claim_id in claim_objects},
        )
        if semantic is None:
            degraded.append(EMBEDDING_FAILED)
        else:
            rankings.extend(semantic)

    covered: dict[str, str] = {}
    for slug in _ranked(page_scores):
        matched_claim = page_objects[slug].get("matched_claim")
        if matched_claim:
            covered.setdefault(str(matched_claim["claim_id"]), slug)

    seen_pages: set[str] = set()
    kept: list[tuple[str, float]] = []
    for identifier, score in rrf_rank(*rankings):
        kind, _, key = identifier.partition(":")
        if kind == "claim":
            if key in covered:
                continue
            kept.append((identifier, score))
            continue
        if key in seen_pages:
            continue
        page = page_objects.get(key)
        if page is None:
            continue
        seen_pages.add(key)
        kept.append((identifier, score))

    results: list[dict[str, Any]] = []
    for identifier, score in kept[: max(int(limit), 0)]:
        kind, _, key = identifier.partition(":")
        if kind == "claim":
            results.append(
                _claim_result(
                    store=store,
                    scope=scope,
                    claim=claim_objects[key],
                    matched=claim_matched.get(key, []),
                    score=score,
                    snapshot_version=snapshot_version,
                )
            )
        else:
            results.append(
                _page_result(
                    store=store,
                    scope=scope,
                    page=page_objects[key],
                    matched=page_matched.get(key, []),
                    score=score,
                    snapshot_version=snapshot_version,
                )
            )

    return {
        "results": results,
        "degraded": bool(degraded),
        "degraded_reason": "; ".join(degraded),
        "snapshot_version": snapshot_version,
    }


def build_answer_context(
    *,
    store: Any,
    scope: Scope,
    results: Sequence[Mapping[str, Any]],
    allow_source_fallback: bool = False,
) -> dict[str, Any]:
    """The model-facing context, assembled from claims.

    A result that names a claim contributes the claim's own statement, conditions,
    status and citation. Source text enters only when the caller allowed the
    fallback, and then it is flagged per item and on the answer as a whole: a
    question the knowledge layer can answer never drags artifact text along.
    """

    versions = {str(row["claim_version_id"]): row for row in store.iter_claims(scope, include_history=True)}
    items: list[dict[str, Any]] = []
    dropped_claims: list[str] = []
    dropped_source_items: list[str] = []
    used_source_text = False
    for result in results or ():
        object_type = str(result.get("object_type") or "")
        if object_type == "source":
            if not allow_source_fallback:
                dropped_source_items.append(str(result.get("artifact_id") or ""))
                continue
            items.append(_source_context_item(result))
            used_source_text = True
            continue
        claim_id = str(result.get("claim_id") or "")
        version = versions.get(str(result.get("claim_version_id") or ""))
        if version is None and claim_id:
            version = _current_version_of(versions, claim_id)
        if version is None:
            dropped_claims.append(claim_id)
            continue
        items.append(
            {
                "object_type": object_type or "claim",
                "claim_id": str(version["claim_id"]),
                "claim_version_id": str(version["claim_version_id"]),
                "version": int(version.get("version") or 0),
                "page_slug": str(result.get("page_slug") or ""),
                "statement": str(version.get("statement") or ""),
                "conditions": [str(item) for item in version.get("conditions") or ()],
                "knowledge_status": _status(version),
                "labels": labels_for_claim(version),
                "citation": list(result.get("citation") or _evidence_ids(store, scope, str(version["claim_id"]))),
                "matched_text": str(result.get("matched_text") or ""),
                "source_text_included": False,
                "raw_derived": False,
            }
        )

    return {
        "items": items,
        "used_source_text": used_source_text,
        "allow_source_fallback": bool(allow_source_fallback),
        "excluded_source_items": dropped_source_items,
        "dropped_claims": dropped_claims,
        "snapshot_version": store.current_version(scope.project_id),
    }


def explain_claim(
    *,
    store: Any,
    scope: Scope,
    claim_id: str,
    mode: str = MODE_WHY,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Why a claim holds, with recorded reasons and rebuilt ones kept apart.

    `recorded` holds reasons the material states: a rationale claim whose
    derivation is explicit. `reconstructed` holds what the system derived, notes
    and assumptions included. A chain deeper than `max_depth` comes back with
    `truncated` and the claim versions that were not expanded, because a chain
    that stops silently reads as a chain that ended.
    """

    if mode not in (MODE_WHY, MODE_HOW):
        raise KnowledgeError(f"Unknown explain mode: {mode!r}.", code="INVALID_STATE")
    if not isinstance(max_depth, int) or max_depth < 0:
        raise KnowledgeError("max_depth must be a non-negative integer.", code="INVALID_STATE")

    versions = {str(row["claim_version_id"]): row for row in store.iter_claims(scope, include_history=True)}
    detail = store.get_claim(claim_id, scope)
    root = versions.get(str(detail["current_version_id"]))
    if root is None:
        raise ClaimStoreError(f"Claim {claim_id} has no committed version.")

    edges = _reason_edges(store=store, scope=scope, mode=mode)
    chain: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []
    reconstructed: list[dict[str, Any]] = []
    continue_from: list[dict[str, Any]] = []
    expanded: set[str] = set()
    origins_cache: dict[str, list[dict[str, Any]]] = {}
    if mode == MODE_HOW:
        chain.extend(_procedure_entries(root))

    queue: list[tuple[str, int, str, str]] = [(str(root["claim_version_id"]), 0, "root", "")]
    position = 0
    while position < len(queue):
        version_id, depth, role, relation_type = queue[position]
        position += 1
        if version_id in expanded:
            continue
        expanded.add(version_id)
        row = versions.get(version_id)
        if row is None:
            continue
        origins = origins_cache.setdefault(version_id, _origins_of(store, scope, row))
        neighbours = _neighbours(row=row, origins=origins, edges=edges)
        entry = _chain_entry(row, depth=depth, role=role, relation_type=relation_type)

        if depth > 0 and str(row.get("knowledge_kind") or "") == "rationale" and str(row.get("derivation") or "") == "explicit":
            entry["reason_kind"] = "recorded"
            recorded.append(_recorded_entry(row, origins, depth=depth, relation_type=relation_type))
        elif str(row.get("derivation") or "") == "synthesized":
            entry["reason_kind"] = "reconstructed"
            synthesized = [origin for origin in origins if str(origin.get("derivation") or "") == "synthesized"]
            for origin in synthesized or [None]:
                reconstructed.append(_reconstructed_entry(row, origin))
        chain.append(entry)

        if depth >= max_depth:
            for neighbour_relation, neighbour in neighbours:
                beyond = versions.get(neighbour)
                if beyond is None:
                    continue
                if any(item["claim_version_id"] == neighbour for item in continue_from):
                    continue
                continue_from.append(
                    _chain_entry(beyond, depth=depth + 1, role="unexpanded", relation_type=neighbour_relation)
                )
            continue
        for neighbour_relation, neighbour in neighbours:
            queue.append((neighbour, depth + 1, _ROLE_FOR_RELATION.get(neighbour_relation, "related"), neighbour_relation))

    return {
        "claim_id": str(claim_id),
        "claim_version_id": str(root["claim_version_id"]),
        "mode": mode,
        "max_depth": max_depth,
        "chain": chain,
        "recorded": recorded,
        "reconstructed": reconstructed,
        "truncated": bool(continue_from),
        "continue_from": continue_from,
        "snapshot_version": store.current_version(scope.project_id),
    }


def quote_evidence(*, store: Any, scope: Scope, evidence_id: str) -> dict[str, Any]:
    """The exact source text one citation names, recovered from the artifact.

    Never from a page: a page is a projection that can be rewritten, so text a
    reader is told is a quote comes back through the evidence address and its
    recorded span hashes. A citation that cannot resolve comes back as the error
    code that says why, and with no text at all.
    """

    try:
        recovered = store.load_evidence(evidence_id, scope)
    except EvidenceError as error:
        return {
            "evidence_id": evidence_id,
            "found": False,
            "error_code": error.code,
            "reason": str(error),
            "exact_text": None,
            "segments": [],
            "span_hashes": [],
            "quote_origin": "",
            "offset_unit": OFFSET_UNIT,
        }
    return {
        **recovered.as_dict(),
        "found": True,
        "error_code": "",
        "quote_origin": "artifact",
        "offset_unit": OFFSET_UNIT,
    }


def get_as_of(claims: Sequence[Mapping[str, Any]], as_of: str | None) -> dict[str, Any]:
    """Which claims a recorded time puts in effect, and which have no recorded time.

    A claim whose `asserted_at` is unknown is not placed at the query time: "we do
    not know when this was said" and "this was said when you asked" are different
    answers, and only one of them is true.
    """

    in_effect: list[dict[str, Any]] = []
    time_unknown: list[dict[str, Any]] = []
    recorded_after: list[dict[str, Any]] = []
    for claim in claims or ():
        version = claim_version_row(claim)
        entry = _time_entry(version)
        asserted_at = version.get("asserted_at")
        if not asserted_at:
            time_unknown.append(entry)
        elif as_of and str(asserted_at) > str(as_of):
            recorded_after.append(entry)
        else:
            in_effect.append(entry)
    in_effect.sort(key=_time_order)
    recorded_after.sort(key=_time_order)
    time_unknown.sort(key=lambda item: item["claim_version_id"])
    return {
        "as_of": as_of,
        "in_effect": in_effect,
        "time_unknown": time_unknown,
        "recorded_after": recorded_after,
    }


def current_versus_historical(
    *,
    store: Any,
    scope: Scope,
    claim_id: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    """What the project holds now, and what it held at a recorded time.

    Supersession is followed in both directions: a claim that replaced an earlier
    one answers the earlier one's question, so both are read together and neither
    is presented as the other. A version with no recorded time is reported as
    time-unknown and never dated at the query time.
    """

    chain_claim_ids = _supersession_chain(store=store, scope=scope, claim_id=str(claim_id))
    chain_rows = [
        row
        for row in store.iter_claims(scope, include_history=True)
        if str(row["claim_id"]) in chain_claim_ids
    ]
    if not chain_rows:
        raise ClaimStoreError(f"Claim {claim_id} has no committed version.")

    newest: dict[str, Mapping[str, Any]] = {}
    for row in chain_rows:
        key = str(row["claim_id"])
        if key not in newest or int(row.get("version") or 0) > int(newest[key].get("version") or 0):
            newest[key] = row
    active = [
        row for row in newest.values() if str(row.get("lifecycle_status") or "") == "active"
    ]
    active.sort(key=lambda row: (str(row.get("asserted_at") or ""), str(row["claim_version_id"])))
    current = _time_entry(active[-1]) if active else None

    bucket = get_as_of(chain_rows, as_of)
    if as_of is None:
        selected = current
    else:
        selected = bucket["in_effect"][-1] if bucket["in_effect"] else None

    summaries = {str(entry["claim_version_id"]): entry for entry in bucket["in_effect"]}
    summaries.update({str(entry["claim_version_id"]): entry for entry in bucket["recorded_after"]})
    summaries.update({str(entry["claim_version_id"]): entry for entry in bucket["time_unknown"]})
    historical = [entry for entry in summaries.values() if str(entry["lifecycle_status"]) != "active"]
    if as_of is not None:
        historical.extend(bucket["recorded_after"])
    historical_by_version = {str(entry["claim_version_id"]): entry for entry in historical}
    if selected is not None:
        historical_by_version.pop(str(selected["claim_version_id"]), None)

    reason = _selection_reason(as_of=as_of, selected=selected, time_unknown=bucket["time_unknown"])
    return {
        "claim_id": str(claim_id),
        "as_of": as_of,
        "current": current,
        "selected": selected,
        "historical": sorted(historical_by_version.values(), key=_time_order),
        "time_unknown": bucket["time_unknown"],
        "chain": sorted(chain_claim_ids),
        "reason": reason,
        "snapshot_version": store.current_version(scope.project_id),
    }


def source_fallback(
    *,
    store: Any,
    scope: Scope,
    query: str,
    claim_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """On-demand source text, for when the knowledge layer lacks the detail.

    Every item says `raw_derived`, because none of this text passed validation on
    the way in: it is what the frozen artifact says, found now, and it carries no
    evidence id because no citation was issued for it. When nothing matches, the
    answer is a reason, never a paraphrase of something that was not there.
    """

    snapshot_version = store.current_version(scope.project_id)
    known = len(claim_results or ())
    counts = _counts(tokenize(query))
    if not counts:
        return {
            "found": False,
            "items": [],
            "raw_derived": True,
            "reason": "empty_query",
            "known_from_claims": known,
            "snapshot_version": snapshot_version,
        }

    artifacts = _artifact_texts(store, scope)
    if artifacts is None:
        return {
            "found": False,
            "items": [],
            "raw_derived": True,
            "reason": "source_text_unavailable",
            "known_from_claims": known,
            "snapshot_version": snapshot_version,
        }

    items: list[dict[str, Any]] = []
    for artifact in artifacts:
        text = str(artifact.get("normalized_text") or "")
        for window in _matching_windows(text, counts):
            start, end = window["span"]
            items.append(
                {
                    "object_type": "source",
                    "project_id": scope.project_id,
                    "artifact_id": str(artifact["artifact_id"]),
                    "revision_id": str(artifact["revision_id"]),
                    "source_id": str(artifact["source_id"]),
                    "source_label": str(artifact.get("source_label") or ""),
                    "evidence_id": "",
                    "spans": [[start, end]],
                    "offset_unit": OFFSET_UNIT,
                    "matched_text": text[start:end],
                    "raw_derived": True,
                    "score": window["score"],
                    "snapshot_version": snapshot_version,
                }
            )
    items.sort(key=lambda item: (-item["score"], item["artifact_id"], item["spans"][0][0]))

    if not items:
        return {
            "found": False,
            "items": [],
            "raw_derived": True,
            "reason": "no_artifact_text_matches_query",
            "known_from_claims": known,
            "snapshot_version": snapshot_version,
        }
    return {
        "found": True,
        "items": items[:MAX_SOURCE_ITEMS],
        "raw_derived": True,
        "reason": (
            "raw_text_beyond_the_knowledge_layer" if known else "raw_text_with_no_claim_covering_the_query"
        ),
        "known_from_claims": known,
        "snapshot_version": snapshot_version,
    }


def renderable_claims(
    store: Any,
    scope: Scope,
    *,
    claim_ids: Sequence[str] | None = None,
    include_history: bool = False,
) -> list[dict[str, Any]]:
    """Claim versions shaped for the projection: version row plus origins and relations.

    `iter_claims` answers with the version row alone, and a page needs more than
    that: a synthesized paragraph carries the origin's inference note, and a
    rationale needs the relation that says what it attaches to. This is the one
    place that joins the two, so the projection itself stays a pure function of the
    dicts it is given.
    """

    wanted = {str(claim_id) for claim_id in claim_ids} if claim_ids is not None else None
    rows = [
        row
        for row in store.iter_claims(scope, include_history=include_history)
        if wanted is None or str(row["claim_id"]) in wanted
    ]
    enriched: list[dict[str, Any]] = []
    for row in rows:
        detail = store.get_claim(str(row["claim_id"]), scope, version=int(row.get("version") or 0))
        enriched.append({**row, "origins": detail["origins"], "relations": detail["relations"]})
    return enriched


def _counts(tokens: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


def _ranked(scores: Mapping[str, float]) -> list[str]:
    return sorted((key for key in scores if scores[key] > 0), key=lambda key: (-scores[key], key))


def _claim_text(claim: Mapping[str, Any]) -> str:
    conditions = "\n".join(str(item) for item in claim.get("conditions") or ())
    return f"{claim.get('statement') or ''}\n{conditions}"


def _match_score(counts: Mapping[str, int], text: str, *, title: str = "") -> tuple[float, list[str]]:
    """A deterministic lexical score: how much of the query the text carries."""

    if not counts:
        return 0.0, []
    present = _counts(tokenize(text))
    if not present:
        return 0.0, []
    matched = [token for token in counts if token in present]
    if not matched:
        return 0.0, []
    score = 4.0 * len(matched) + float(min(sum(present[token] for token in matched), 20))
    if title:
        title_tokens = set(tokenize(title))
        score += 6.0 * len([token for token in counts if token in title_tokens])
    return score, matched


def _best_claim(counts: Mapping[str, int], candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    best: Mapping[str, Any] | None = None
    best_score = 0.0
    for claim in candidates:
        score, _ = _match_score(counts, str(claim.get("statement") or ""))
        if score <= 0:
            continue
        if best is None or score > best_score or (score == best_score and str(claim["claim_id"]) < str(best["claim_id"])):
            best, best_score = claim, score
    return best


def _snippet(text: str, matched: Sequence[str], *, limit: int = MAX_MATCHED_TEXT) -> str:
    """The line that carries the match. A reading aid, never a quote."""

    matched_set = set(matched)
    for line in str(text if text is not None else "").splitlines():
        stripped = line.strip()
        if stripped and matched_set.intersection(tokenize(stripped)):
            return stripped[:limit]
    return ""


def _status(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "knowledge_kind": str(row.get("knowledge_kind") or ""),
        "derivation": str(row.get("derivation") or ""),
        "epistemic_status": str(row.get("epistemic_status") or ""),
        "lifecycle_status": str(row.get("lifecycle_status") or ""),
        "grounding_status": str(row.get("grounding_status") or ""),
        "decision_state": row.get("decision_state"),
        "question_state": row.get("question_state"),
    }


def _evidence_ids(store: Any, scope: Scope, claim_id: str) -> list[str]:
    """The evidence the claim's current version cites, in recorded order."""

    try:
        detail = store.get_claim(claim_id, scope)
    except (ClaimStoreError, ValueError):
        return []
    found: list[str] = []
    for origin in detail.get("origins", ()):
        for evidence_id in origin.get("evidence_refs", ()):
            if evidence_id not in found:
                found.append(str(evidence_id))
    return found


def _claim_result(
    *,
    store: Any,
    scope: Scope,
    claim: Mapping[str, Any],
    matched: Sequence[str],
    score: float,
    snapshot_version: int,
) -> dict[str, Any]:
    return {
        "object_type": "claim",
        "project_id": scope.project_id,
        "claim_id": str(claim["claim_id"]),
        "claim_version_id": str(claim["claim_version_id"]),
        "version": int(claim.get("version") or 0),
        "page_slug": "",
        "matched_text": _snippet(str(claim.get("statement") or ""), matched),
        "statement": str(claim.get("statement") or ""),
        "knowledge_status": _status(claim),
        "labels": labels_for_claim(claim),
        "citation": _evidence_ids(store, scope, str(claim["claim_id"])),
        "snapshot_version": snapshot_version,
        "score": score,
    }


def _page_result(
    *,
    store: Any,
    scope: Scope,
    page: Mapping[str, Any],
    matched: Sequence[str],
    score: float,
    snapshot_version: int,
) -> dict[str, Any]:
    matched_claim = page.get("matched_claim")
    citation = _evidence_ids(store, scope, str(matched_claim["claim_id"])) if matched_claim else []
    return {
        "object_type": "page",
        "project_id": scope.project_id,
        "page_slug": str(page["slug"]),
        "title": str(page.get("title") or ""),
        "claim_id": str(matched_claim["claim_id"]) if matched_claim else "",
        "claim_version_id": str(matched_claim["claim_version_id"]) if matched_claim else "",
        "version": int(matched_claim.get("version") or 0) if matched_claim else 0,
        "matched_text": _snippet(page["markdown"], matched),
        "statement": str(matched_claim.get("statement") or "") if matched_claim else "",
        "knowledge_status": _status(matched_claim) if matched_claim else {},
        "labels": labels_for_claim(matched_claim) if matched_claim else [],
        "citation": citation,
        "snapshot_version": snapshot_version,
        "projection_status": str(page.get("projection_status") or "current"),
        "content_source": str(page.get("content_source") or STORED_CONTENT_SOURCE),
        "content_sha256": str(page.get("content_sha256") or ""),
        "score": score,
    }


def _current_version_of(versions: Mapping[str, Mapping[str, Any]], claim_id: str) -> Mapping[str, Any] | None:
    candidates = [row for row in versions.values() if str(row.get("claim_id") or "") == str(claim_id)]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row.get("version") or 0))


def _effective_page(
    *,
    row: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    degraded: list[str],
) -> dict[str, Any]:
    """The page text a query may read. A stale row is re-rendered, never served.

    The stored markdown of a page whose claim set moved, whose renderer changed and
    which the store marked dirty is old knowledge presented as new. The fallback
    template is built from the current claims instead, and the caller sees which
    version of the page it got.
    """

    manifest = _manifest_of(row)
    renderer_version = str(manifest.get("renderer_version") or row.get("renderer_version") or "")
    outdated = bool(renderer_version) and renderer_version != RENDERER_VERSION
    stale = (
        bool(row.get("dirty"))
        or str(row.get("projection_status") or "") not in ("", "current")
        or outdated
    )
    manifest_claim_ids = {
        str(entry.get("claim_id"))
        for entry in manifest.get("entries") or ()
        if isinstance(entry, Mapping) and entry.get("claim_id")
    }
    page_claims = [claim for claim in claims if str(claim.get("claim_id") or "") in manifest_claim_ids]
    if not stale:
        return {
            "slug": str(row["slug"]),
            "title": str(row.get("title") or ""),
            "markdown": str(row.get("markdown") or ""),
            "claims": page_claims,
            "projection_status": str(row.get("projection_status") or "current"),
            "content_source": STORED_CONTENT_SOURCE,
            "content_sha256": str(row.get("content_sha256") or ""),
        }
    page = stale_projection_fallback(claims=page_claims, page_slug=str(row["slug"]), title=str(row.get("title") or ""))
    reason = RENDERER_OUTDATED if outdated else STALE_PROJECTION_PRESENT
    if reason not in degraded:
        degraded.append(reason)
    return {
        "slug": str(row["slug"]),
        "title": str(row.get("title") or ""),
        "markdown": page["markdown"],
        "claims": page_claims,
        "projection_status": STALE_PAGE_STATUS,
        "content_source": FALLBACK_CONTENT_SOURCE,
        "content_sha256": page["content_sha256"],
    }


def _manifest_of(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("manifest_json")
    if isinstance(row.get("manifest"), Mapping):
        return dict(row["manifest"])
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _store_rows(store: Any, sql: str, parameters: Sequence[Any]) -> list[dict[str, Any]] | None:
    """Rows the store has no public reader for, read read-only, or None.

    The v2 store owns `page_projections` and `parsed_artifacts` but exposes no
    reader for either, and retrieval must not guess at their content from memory.
    A missing database, a missing table or a busy file returns None, which the
    caller reports as a degraded channel instead of an empty answer.
    """

    database = getattr(store, "database", None)
    if not database:
        return None
    path = Path(str(database))
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, tuple(parameters))]
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _page_rows(store: Any, scope: Scope) -> list[dict[str, Any]] | None:
    columns = scope_columns(scope)
    return _store_rows(
        store,
        """SELECT slug, title, page_kind, renderer_version, manifest_json, markdown,
                  content_sha256, projection_status, dirty, rendered_at
           FROM page_projections
           WHERE knowledge_space_id = ? AND project_id = ?
           ORDER BY slug""",
        (columns["knowledge_space_id"], columns["project_id"]),
    )


def _artifact_texts(store: Any, scope: Scope) -> list[dict[str, Any]] | None:
    """Artifact text for one project, with withdrawn material left out.

    A withdrawn source keeps its artifacts, but its text no longer informs an
    answer: the same refusal `load_evidence` makes, applied to raw text found on
    demand.
    """

    columns = scope_columns(scope)
    return _store_rows(
        store,
        """SELECT a.artifact_id, a.revision_id, a.normalized_text, s.source_id, s.label AS source_label
           FROM parsed_artifacts a
           JOIN source_revisions r ON r.revision_id = a.revision_id
           JOIN sources s ON s.source_id = r.source_id
           WHERE a.knowledge_space_id = ? AND a.project_id = ?
             AND r.withdrawn_at IS NULL AND s.withdrawn_at IS NULL
           ORDER BY a.artifact_id""",
        (columns["knowledge_space_id"], columns["project_id"]),
    )


def _matching_windows(text: str, counts: Mapping[str, int]) -> list[dict[str, Any]]:
    """Line windows of an artifact that carry the query, addressed in code points."""

    spans = line_spans(text)
    scored: list[tuple[int, float]] = []
    for index, (start, end) in enumerate(spans):
        score, _ = _match_score(counts, text[start:end])
        if score > 0:
            scored.append((index, score))
    windows: list[dict[str, Any]] = []
    for index, score in scored:
        if windows and index - windows[-1]["last"] == 1:
            window = windows[-1]
            window["last"] = index
            window["score"] += score
            continue
        windows.append({"first": index, "last": index, "score": score})
    for window in windows:
        window["span"] = (spans[window["first"]][0], spans[window["last"]][1])
    return windows


def _embedding_rankings(
    *,
    embedding: Any,
    query: str,
    pages: Mapping[str, str],
    claims: Mapping[str, str],
) -> list[list[str]] | None:
    """Optional semantic recall, or None when the channel could not be used.

    The channel is called once per candidate and once for the query. A caller that
    keeps a vector cache wraps the channel, because caching is a property of the
    provider and not of this merge.
    """

    try:
        query_vector = _vector(embedding(query))
        if query_vector is None:
            return None
        rankings: list[list[str]] = []
        for documents in (pages, claims):
            scored: list[tuple[str, float]] = []
            for identifier, text in documents.items():
                vector = _vector(embedding(text))
                if vector is None or len(vector) != len(query_vector):
                    return None
                similarity = _cosine(query_vector, vector)
                if similarity > 0:
                    scored.append((identifier, similarity))
            scored.sort(key=lambda item: (-item[1], item[0]))
            rankings.append([identifier for identifier, _ in scored])
        return rankings
    except Exception:
        return None


def _vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return vector or None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _reason_edges(*, store: Any, scope: Scope, mode: str) -> dict[str, list[tuple[str, str]]]:
    """Reason edges per claim version, oriented so the neighbour is the reason.

    `derived_from` and `depends_on` run from the claim to what it leans on.
    `supports` runs the other way: the premise supports the supported claim, so the
    reason is attached to the claim being supported.
    """

    edges: dict[str, list[tuple[str, str]]] = {}
    for relation_type in (_HOW_RELATION_TYPES if mode == MODE_HOW else _WHY_RELATION_TYPES):
        for relation in store.relations_of_type(relation_type, scope):
            if str(relation.get("relation_status") or "") == "retracted":
                continue
            source = str(relation.get("from_claim_version_id") or "")
            target = str(relation.get("to_claim_version_id") or "")
            if not source or not target:
                continue
            if relation_type == "supports":
                edges.setdefault(target, []).append((relation_type, source))
            else:
                edges.setdefault(source, []).append((relation_type, target))
    return edges


def _neighbours(
    *,
    row: Mapping[str, Any],
    origins: Sequence[Mapping[str, Any]],
    edges: Mapping[str, Sequence[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """Reason edges recorded as relations, plus the premises an origin recorded.

    A synthesized origin names its premises whether or not a relation row was ever
    written for them, so both sources are read and duplicates collapse.
    """

    version_id = str(row.get("claim_version_id") or "")
    pairs: list[tuple[str, str]] = list(edges.get(version_id, ()))
    for origin in origins:
        for premise in _origin_premises(origin):
            pairs.append(("derived_from", premise))
    ordered: list[tuple[str, str]] = []
    for pair in pairs:
        if pair[1] == version_id or pair in ordered:
            continue
        ordered.append(pair)
    return ordered


def _origin_premises(origin: Mapping[str, Any]) -> list[str]:
    """The premise claim versions an origin recorded, from either read shape.

    `get_claim` answers with support requirements, while a dict carried over from
    an earlier read uses the contract's own `premise_claim_version_ids`. Reading
    both is what keeps a premise from disappearing between the two.
    """

    premises = [str(value) for value in origin.get("premise_claim_version_ids") or ()]
    for requirement in origin.get("support_requirements") or ():
        if not isinstance(requirement, Mapping):
            continue
        if str(requirement.get("kind") or "") != "claim_version":
            continue
        value = requirement.get("id")
        if value and str(value) not in premises:
            premises.append(str(value))
    return premises


def _origins_of(store: Any, scope: Scope, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        detail = store.get_claim(str(row["claim_id"]), scope, version=int(row.get("version") or 0))
    except (ClaimStoreError, ValueError):
        return []
    return [origin for origin in detail.get("origins", ()) if isinstance(origin, Mapping)]


def _chain_entry(
    row: Mapping[str, Any],
    *,
    depth: int,
    role: str,
    relation_type: str,
) -> dict[str, Any]:
    return {
        "claim_id": str(row.get("claim_id") or ""),
        "claim_version_id": str(row.get("claim_version_id") or ""),
        "version": int(row.get("version") or 0),
        "statement": str(row.get("statement") or ""),
        "knowledge_kind": str(row.get("knowledge_kind") or ""),
        "derivation": str(row.get("derivation") or ""),
        "epistemic_status": str(row.get("epistemic_status") or ""),
        "lifecycle_status": str(row.get("lifecycle_status") or ""),
        "grounding_status": str(row.get("grounding_status") or ""),
        "decision_state": row.get("decision_state"),
        "asserted_at": row.get("asserted_at"),
        "reason_kind": "",
        "depth": depth,
        "role": role,
        "relation_type": relation_type,
    }


def _recorded_entry(
    row: Mapping[str, Any],
    origins: Sequence[Mapping[str, Any]],
    *,
    depth: int,
    relation_type: str,
) -> dict[str, Any]:
    citation: list[str] = []
    for origin in origins:
        for evidence_id in origin.get("evidence_refs") or ():
            if str(evidence_id) not in citation:
                citation.append(str(evidence_id))
    return {
        "claim_id": str(row.get("claim_id") or ""),
        "claim_version_id": str(row.get("claim_version_id") or ""),
        "version": int(row.get("version") or 0),
        "statement": str(row.get("statement") or ""),
        "derivation": "explicit",
        "depth": depth,
        "relation_type": relation_type,
        "citation": citation,
    }


def _reconstructed_entry(row: Mapping[str, Any], origin: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "claim_id": str(row.get("claim_id") or ""),
        "claim_version_id": str(row.get("claim_version_id") or ""),
        "version": int(row.get("version") or 0),
        "statement": str(row.get("statement") or ""),
        "derivation": "synthesized",
        "inference_note": str((origin or {}).get("inference_note") or ""),
        "assumptions": [str(item) for item in (origin or {}).get("assumptions") or ()],
        "premises": _origin_premises(origin) if origin else [],
        "origin_id": str((origin or {}).get("origin_id") or ""),
    }


def _procedure_entries(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A process's recorded steps, in the order the claim recorded them."""

    attributes = row.get("attributes") if isinstance(row.get("attributes"), Mapping) else {}
    entries: list[dict[str, Any]] = []
    for position, step in enumerate(attributes.get("steps") or (), start=1):
        if not isinstance(step, Mapping):
            continue
        entries.append(
            {
                "claim_id": str(row.get("claim_id") or ""),
                "claim_version_id": str(row.get("claim_version_id") or ""),
                "role": "step",
                "depth": 0,
                "relation_type": "",
                "reason_kind": "",
                "order": int(step.get("order") or position),
                "statement": str(step.get("action") or ""),
                "inputs": [str(item) for item in step.get("inputs") or ()],
                "outputs": [str(item) for item in step.get("outputs") or ()],
                "preconditions": [str(item) for item in step.get("preconditions") or ()],
                "exceptions": [str(item) for item in step.get("exceptions") or ()],
            }
        )
    for position, component in enumerate(attributes.get("components") or (), start=1):
        entries.append(
            {
                "claim_id": str(row.get("claim_id") or ""),
                "claim_version_id": str(row.get("claim_version_id") or ""),
                "role": "component",
                "depth": 0,
                "relation_type": "",
                "reason_kind": "",
                "order": position,
                "statement": str(component),
            }
        )
    return entries


def _supersession_chain(*, store: Any, scope: Scope, claim_id: str) -> set[str]:
    """Every claim linked to this one by supersession, in either direction."""

    relations = store.relations_of_type("supersedes", scope)
    claim_ids = {str(claim_id)}
    frontier = [str(claim_id)]
    while frontier:
        current = frontier.pop()
        for relation in relations:
            if str(relation.get("relation_status") or "") == "retracted":
                continue
            source = str(relation.get("from_claim_id") or "")
            target = str(relation.get("to_claim_id") or "")
            neighbour = ""
            if current == source:
                neighbour = target
            elif current == target:
                neighbour = source
            if neighbour and neighbour not in claim_ids:
                claim_ids.add(neighbour)
                frontier.append(neighbour)
    return claim_ids


def _time_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    asserted_at = row.get("asserted_at")
    return {
        "claim_id": str(row.get("claim_id") or ""),
        "claim_version_id": str(row.get("claim_version_id") or ""),
        "version": int(row.get("version") or 0),
        "statement": str(row.get("statement") or ""),
        "knowledge_kind": str(row.get("knowledge_kind") or ""),
        "epistemic_status": str(row.get("epistemic_status") or ""),
        "lifecycle_status": str(row.get("lifecycle_status") or ""),
        "grounding_status": str(row.get("grounding_status") or ""),
        "decision_state": row.get("decision_state"),
        "asserted_at": asserted_at,
        "asserted_at_precision": str(row.get("asserted_at_precision") or "unknown"),
        "time_unknown": not asserted_at,
        "committed_at": str(row.get("committed_at") or ""),
    }


def _time_order(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (str(entry.get("asserted_at") or ""), str(entry.get("claim_version_id") or ""))


def _selection_reason(*, as_of: str | None, selected: Mapping[str, Any] | None, time_unknown: Sequence[Any]) -> str:
    if as_of is None:
        return "current_knowledge" if selected else "no_active_claim_in_the_chain"
    if selected is None:
        return "no_version_has_a_recorded_time_at_or_before_as_of"
    if time_unknown:
        return "selected_latest_recorded_time_at_or_before_as_of_with_time_unknown_versions_left_out"
    return "selected_latest_recorded_time_at_or_before_as_of"


def _source_context_item(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "object_type": "source",
        "artifact_id": str(result.get("artifact_id") or ""),
        "revision_id": str(result.get("revision_id") or ""),
        "source_id": str(result.get("source_id") or ""),
        "evidence_id": str(result.get("evidence_id") or ""),
        "spans": [list(span) for span in result.get("spans") or ()],
        "matched_text": str(result.get("matched_text") or ""),
        "source_text_included": True,
        "raw_derived": True,
    }
