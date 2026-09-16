"""Reusable, side-effect-free contracts for turning evidence into Wiki updates."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

CORE_SCHEMA_VERSION = 1
# Context window of the configured model. One submit sends the whole project snapshot,
# the materials and the model's own response in a single call, so the per-request budgets
# below have to sum under this. A CJK character is roughly one token, so read the
# character budgets as tokens.
MODEL_CONTEXT_TOKENS = 1_000_000
MAX_MATERIAL_CHARS = 180_000
MAX_OUTPUT_CHARS = 240_000
MAX_PAGE_BODY_CHARS = 100_000
# One material's body ceiling. It deliberately equals MAX_PAGE_BODY_CHARS, but the material
# contract is no longer expressed as "the page body limit applied by default".
MAX_MATERIAL_CONTENT_CHARS = 100_000
MAX_EXISTING_PAGES = 500
MAX_EXISTING_CHARS = 500_000
# Above this snapshot size a submit routing the whole project through the model costs more
# than sending a catalog and reading back only the affected pages. Below it the extra round
# trip is pure overhead, so a young project submits its full text directly.
DIRECT_SUBMIT_MAX_CHARS = 40_000
# A catalog row carries identity only, but it still costs its JSON envelope on top of the
# five fields. 300 characters per entry against MAX_EXISTING_PAGES leaves room to spare.
MAX_CATALOG_CHARS = 200_000
# The routing call answers with selected slugs, not page bodies. Each slug is capped at 80
# characters and there can be at most MAX_EXISTING_PAGES of them.
MAX_ROUTE_OUTPUT_CHARS = 50_000
ALLOWED_TYPES = {
    "concept",
    "decision",
    "guide",
    "reference",
    "person",
    "client",
    "process",
    "system",
}
ALLOWED_STATUSES = {"current", "draft", "superseded", "archived"}
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SOURCE_PATTERN = re.compile(r"^[^\s\x00-\x1f]{1,240}$")


class CoreError(ValueError):
    """A caller-actionable contract or model-output error."""


DIRECT = "direct"
ROUTED = "routed"


def submit_mode(page_count: int, snapshot_chars: int) -> str:
    """Choose how one submit reaches the model.

    Two triggers, and only one of them is about cost. The size trigger is the cost
    argument: past `DIRECT_SUBMIT_MAX_CHARS` a full-snapshot submit spends more than a
    catalog submit that reads back only the affected pages. The page trigger is a
    capability argument, not a cost one: past `MAX_EXISTING_PAGES` a direct submit cannot
    run at all, because `normalize_existing_pages` rejects it. Page count is deliberately
    not a second cost knob, since a catalog entry has a floor of roughly 300 characters
    while a page body has none, so a project of many short pages would pay more for the
    catalog than for the snapshot.
    """

    if snapshot_chars > DIRECT_SUBMIT_MAX_CHARS:
        return ROUTED
    if page_count > MAX_EXISTING_PAGES:
        return ROUTED
    return DIRECT


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _clean_string(value: Any, field: str, *, max_chars: int = MAX_PAGE_BODY_CHARS) -> str:
    result = str(value or "").strip()
    if len(result) > max_chars:
        raise CoreError(f"{field} exceeds the {max_chars} character limit.")
    return result


def _source_id(value: Any) -> str:
    source_id = _clean_string(value, "source_id", max_chars=240)
    if not SOURCE_PATTERN.fullmatch(source_id):
        raise CoreError("source_id must be a non-empty, single-line identifier.")
    return source_id


def normalize_material(material: Any) -> dict[str, str]:
    if not isinstance(material, dict):
        raise CoreError("Each material must be an object.")
    forbidden = {"path", "file_path", "root", "command", "args"} & set(material)
    if forbidden:
        raise CoreError("Materials may contain content and source metadata, not paths or commands.")
    source_id = _source_id(material.get("source_id", material.get("id")))
    content = _clean_string(
        material.get("content", material.get("text")),
        "material content",
        max_chars=MAX_MATERIAL_CONTENT_CHARS,
    )
    if not content:
        raise CoreError(f"Material {source_id} must contain content.")
    kind = _clean_string(material.get("kind", "material"), "material kind", max_chars=40)
    label = _clean_string(material.get("label", source_id), "material label", max_chars=240)
    return {"source_id": source_id, "kind": kind, "label": label, "content": content}


def normalize_materials(materials: Iterable[Any]) -> list[dict[str, str]]:
    if isinstance(materials, (str, bytes)) or not isinstance(materials, Iterable):
        raise CoreError("materials must be a list of selected evidence objects.")
    normalized = [normalize_material(item) for item in materials]
    if not normalized:
        raise CoreError("At least one selected material is required.")
    if len({item["source_id"] for item in normalized}) != len(normalized):
        raise CoreError("Material source_id values must be unique.")
    if sum(len(item["content"]) for item in normalized) > MAX_MATERIAL_CHARS:
        raise CoreError(f"Materials exceed the {MAX_MATERIAL_CHARS} character limit.")
    return normalized


def normalize_existing_pages(pages: Iterable[Any]) -> list[dict[str, Any]]:
    if isinstance(pages, (str, bytes)) or not isinstance(pages, Iterable):
        raise CoreError("existing_pages must be a list.")
    result = []
    for index, page in enumerate(pages):
        if index >= MAX_EXISTING_PAGES:
            raise CoreError(f"existing_pages exceeds the {MAX_EXISTING_PAGES} page limit.")
        if not isinstance(page, dict):
            raise CoreError("Each existing page must be an object.")
        slug = _clean_string(page.get("slug"), "page slug", max_chars=80)
        if not SLUG_PATTERN.fullmatch(slug):
            raise CoreError(f"Unsafe or invalid page slug: {slug!r}")
        content = _clean_string(page.get("content", page.get("body", "")), "page content")
        sources = page.get("sources", [])
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list):
            raise CoreError(f"Existing page {slug} sources must be a list.")
        aliases = page.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, list):
            raise CoreError(f"Existing page {slug} aliases must be a list.")
        result.append({
            "slug": slug,
            "content": content,
            "sources": sorted({_source_id(source) for source in sources}),
            "aliases": sorted({_clean_string(alias, "alias", max_chars=120) for alias in aliases if str(alias).strip()}),
        })
    if sum(len(page["content"]) for page in result) > MAX_EXISTING_CHARS:
        raise CoreError(f"existing_pages exceed the {MAX_EXISTING_CHARS} character limit.")
    return result


def validate_page(page: Any, allowed_sources: set[str]) -> dict[str, Any]:
    if not isinstance(page, dict):
        raise CoreError("Every model page must be an object.")
    slug = _clean_string(page.get("slug"), "page slug", max_chars=80)
    if not SLUG_PATTERN.fullmatch(slug):
        raise CoreError(f"Unsafe or invalid page slug: {slug!r}")
    page_type = _clean_string(page.get("type"), "page type", max_chars=40)
    status = _clean_string(page.get("status"), "page status", max_chars=40)
    if page_type not in ALLOWED_TYPES:
        raise CoreError(f"Invalid page type for {slug}: {page_type}")
    if status not in ALLOWED_STATUSES:
        raise CoreError(f"Invalid page status for {slug}: {status}")
    sources = page.get("sources", [])
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list) or not sources:
        raise CoreError(f"Page {slug} must contain at least one source.")
    normalized_sources = sorted({_source_id(source) for source in sources})
    unknown = [source for source in normalized_sources if source not in allowed_sources]
    if unknown:
        raise CoreError(f"Page {slug} cites unknown sources: {', '.join(unknown)}")
    tags = page.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        raise CoreError(f"Page {slug} tags must be a list.")
    aliases = page.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list):
        raise CoreError(f"Page {slug} aliases must be a list.")
    cleaned = {
        "slug": slug,
        "title": _clean_string(page.get("title"), "page title", max_chars=240),
        "type": page_type,
        "status": status,
        "tags": sorted({_clean_string(tag, "tag", max_chars=80) for tag in tags if str(tag).strip()}),
        "summary": _clean_string(page.get("summary"), "page summary", max_chars=4_000),
        "body": _clean_string(page.get("body"), "page body"),
        "sources": normalized_sources,
        "aliases": sorted({_clean_string(alias, "alias", max_chars=120) for alias in aliases if str(alias).strip()}),
        "updated_at": now_iso(),
    }
    if not all(cleaned[key] for key in ("title", "summary", "body")):
        raise CoreError(f"Page {slug} is missing title, summary, or body.")
    return cleaned


Model = Callable[[dict[str, Any], str, list[dict[str, Any]]], dict[str, Any]]


class WikiCore:
    """Build and validate a Wiki update without reading or writing external state."""

    def __init__(self, model: Model):
        self._model = model

    def organize(
        self,
        materials: Iterable[Any],
        existing_pages: Iterable[Any],
        purpose: str,
    ) -> dict[str, Any]:
        normalized_materials = normalize_materials(materials)
        normalized_pages = normalize_existing_pages(existing_pages)
        normalized_purpose = _clean_string(purpose, "purpose", max_chars=8_000)
        if not normalized_purpose:
            raise CoreError("purpose cannot be empty.")
        payload = {
            "materials": normalized_materials,
            "existing_pages": normalized_pages,
        }
        result = self._model(payload, normalized_purpose, normalized_pages)
        if not isinstance(result, dict):
            raise CoreError("Model output must be a JSON object.")
        allowed_sources = {item["source_id"] for item in normalized_materials}
        for page in normalized_pages:
            allowed_sources.update(page["sources"])
        raw_pages = result.get("pages", [])
        if not isinstance(raw_pages, list):
            raise CoreError("Model output field 'pages' must be a list.")
        pages = [validate_page(page, allowed_sources) for page in raw_pages]
        update = {
            "schema_version": CORE_SCHEMA_VERSION,
            "pages": pages,
            "note": _clean_string(result.get("note", "Wiki update prepared."), "note", max_chars=2_000),
            "source_ids": sorted({source for page in pages for source in page["sources"]}),
        }
        provider = result.get("_provider")
        if isinstance(provider, dict):
            update["provider"] = {
                "name": _clean_string(provider.get("name", "configured model"), "provider name", max_chars=120),
                "retention": _clean_string(provider.get("retention", "unknown"), "provider retention", max_chars=240),
            }
        if _json_size(update) > MAX_OUTPUT_CHARS:
            raise CoreError(f"Update package exceeds the {MAX_OUTPUT_CHARS} character limit.")
        return update


def validate_update_package(update: Any, allowed_sources: set[str]) -> dict[str, Any]:
    """Validate a package received from an adapter before it reaches storage."""

    if not isinstance(update, dict) or update.get("schema_version") != CORE_SCHEMA_VERSION:
        raise CoreError("Unsupported or malformed update package.")
    raw_pages = update.get("pages")
    if not isinstance(raw_pages, list):
        raise CoreError("Update package field 'pages' must be a list.")
    pages = [validate_page(page, allowed_sources) for page in raw_pages]
    if len({page["slug"] for page in pages}) != len(pages):
        raise CoreError("Update package cannot contain duplicate page slugs.")
    result = {
        "schema_version": CORE_SCHEMA_VERSION,
        "pages": pages,
        "note": _clean_string(update.get("note", "Wiki update."), "note", max_chars=2_000),
        "source_ids": sorted({_source_id(source) for source in update.get("source_ids", allowed_sources)}),
    }
    unknown = set(result["source_ids"]) - allowed_sources
    if unknown:
        raise CoreError(f"Update package contains unknown source IDs: {', '.join(sorted(unknown))}")
    if _json_size(result) > MAX_OUTPUT_CHARS:
        raise CoreError(f"Update package exceeds the {MAX_OUTPUT_CHARS} character limit.")
    return result
