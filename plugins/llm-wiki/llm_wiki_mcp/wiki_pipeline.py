"""Serial orchestration of one routed submit.

A routed submit routes first and merges second. Each step is a model call against a
different input, and the caller supplies the fetch between them, so this module never
touches storage and can be exercised with plain dictionaries.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .core import WikiCore, validate_routed_pages

FetchPages = Callable[[Iterable[str]], list[dict[str, Any]]]


def run_routed(
    core: WikiCore,
    catalog_entries: list[dict[str, Any]],
    fetch_pages: FetchPages,
    materials: Iterable[Any],
    purpose: str,
) -> dict[str, Any]:
    """Route the materials to affected pages, then merge against only those pages.

    `package` is None when routing selected nothing. That is not a failure: the evidence
    may still describe pages that do not exist yet, and the caller holds the full snapshot
    needed to decide that. Returning None rather than merging against an empty page set
    keeps the routing phase from spending a second call on a decision it cannot make.
    """

    material_list = list(materials)
    selection = core.select_pages(material_list, catalog_entries, purpose)
    selected_slugs = set(selection["slugs"])
    if not selection["slugs"]:
        return {"mode": "routed", "selected": [], "routing_note": selection["note"], "package": None}
    affected = fetch_pages(selection["slugs"])
    package = core.organize(material_list, affected, purpose)
    # The pages withheld from this merge must come back unchanged. Checking the package
    # rather than the model's raw output gates the artifact that is about to be committed.
    validate_routed_pages(
        package["pages"],
        {entry["slug"] for entry in catalog_entries},
        selected_slugs,
    )
    return {
        "mode": "routed",
        "selected": selection["slugs"],
        "routing_note": selection["note"],
        "package": package,
    }
