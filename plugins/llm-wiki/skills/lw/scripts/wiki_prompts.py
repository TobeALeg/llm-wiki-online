"""Model contracts for the phases of a routed submit.

A routed submit asks the model two different questions. The routing question reads a page
catalog and answers with the slugs the materials affect. The merging question reads those
pages in full and answers with the update package. Each question has its own output
contract, so the contract lives here rather than inline in the transport.
"""

from __future__ import annotations

from typing import Any

ROUTE_PHASE = "route"

# The routing answer names existing pages only. A new topic needs no existing page, so an
# empty selection is a legitimate answer and not a failure.
ROUTE_CONTRACT: dict[str, Any] = {
    "slugs": ["existing-page-slug"],
    "note": "short reason for the selection",
}

ROUTE_INSTRUCTIONS = (
    "You are given evidence and a catalog of existing Wiki pages. Answer with the slugs of "
    "the pages this evidence affects. Select every page the evidence would change, and "
    "select nothing when the evidence only supports new pages. Never invent a slug that is "
    "not in the catalog. Return JSON only."
)

# The merging answer reuses the whole-library contract, because a merge decides the same
# thing a direct submit does, only against a smaller set of pages.
MERGE_CONTRACT: dict[str, Any] = {
    "pages": [{
        "slug": "lowercase-hyphenated-slug",
        "title": "string",
        "type": "concept|decision|guide|reference|person|client|process|system",
        "status": "current|draft|superseded|archived",
        "tags": ["string"],
        "summary": "string",
        "body": "Markdown",
        "sources": ["exact submitted source_id"],
        "aliases": ["other names for this topic"],
    }],
    "note": "short update summary",
}


def build_request(
    purpose: str,
    payload: dict[str, Any],
    existing_pages: list[dict[str, Any]],
    *,
    phase: str | None = None,
) -> dict[str, Any]:
    """Shape one model request. Without a phase this is the whole-library request."""

    if phase == ROUTE_PHASE:
        return {
            "wiki_purpose": purpose,
            "phase": ROUTE_PHASE,
            "instructions": ROUTE_INSTRUCTIONS,
            "evidence": payload,
            "page_catalog": existing_pages,
            "output_contract": ROUTE_CONTRACT,
        }
    return {
        "wiki_purpose": purpose,
        "evidence": payload,
        "existing_pages": existing_pages,
        "output_contract": MERGE_CONTRACT,
    }
