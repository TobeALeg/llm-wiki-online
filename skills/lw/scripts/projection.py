"""Deterministic page projection: every paragraph is a committed claim version.

A page is a pure function of a set of committed claim versions. No model call
happens here and no sentence is summarised, so the manifest can name, paragraph
by paragraph, the claim version each one came from. The section order is fixed,
an empty section is omitted, and a claim that is no longer current is rendered
under 历史变化 instead of reading as current knowledge.

The renderer never writes a sentence of its own. A paragraph is the claim's own
statement plus the tags its status axes earn, its conditions, and the inference
note a synthesized origin recorded. That is the whole vocabulary, which is why a
page cannot drift away from the claims it projects.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .knowledge_types import ClaimState, ClaimVersionState, Scope
except ImportError:  # pragma: no cover - the packaged layout
    from knowledge_types import ClaimState, ClaimVersionState, Scope


RENDERER_VERSION = "projection/1"

SECTIONS: tuple[str, ...] = ("定义", "区分", "当前判断", "决定", "流程/架构", "开放问题", "历史变化")
"""The fixed section order. A section with nothing in it is left out entirely."""

SECTION_FOR_KIND: Mapping[str, str] = {
    "definition": "定义",
    "distinction": "区分",
    "fact": "当前判断",
    "judgment": "当前判断",
    "decision": "决定",
    "process": "流程/架构",
    "architecture": "流程/架构",
    "open_question": "开放问题",
}

ATTACHING_KINDS = ("rationale", "constraint")
"""Kinds with no section of their own. They are rendered where they attach."""

DEFAULT_SECTION = "当前判断"
HISTORY_SECTION = "历史变化"

ATTACHMENT_PRIORITY: Mapping[str, int] = {
    "supports": 0,
    "refines": 1,
    "derived_from": 2,
    "depends_on": 3,
    "supersedes": 4,
    "contradicts": 5,
}

LABELS: Mapping[tuple[str, str], str] = {
    ("lifecycle_status", "superseded"): "（已替代）",
    ("lifecycle_status", "retracted"): "（已撤回）",
    ("decision_state", "proposed"): "（提议，未采纳）",
    ("decision_state", "adopted"): "（已采纳）",
    ("decision_state", "rejected"): "（已否决）",
    ("epistemic_status", "hypothesis"): "（假设）",
    ("epistemic_status", "disputed"): "（有争议）",
    ("epistemic_status", "verified"): "（已核验）",
    ("grounding_status", "needs_revalidation"): "（依据待复核）",
    ("grounding_status", "unsupported"): "（依据不足）",
    ("derivation", "synthesized"): "（系统推导）",
    ("question_state", "resolved"): "（已解决）",
    ("question_state", "deferred"): "（暂缓）",
}

LABEL_ORDER: tuple[tuple[str, str], ...] = (
    ("lifecycle_status", "retracted"),
    ("lifecycle_status", "superseded"),
    ("decision_state", "proposed"),
    ("decision_state", "adopted"),
    ("decision_state", "rejected"),
    ("epistemic_status", "hypothesis"),
    ("epistemic_status", "disputed"),
    ("epistemic_status", "verified"),
    ("grounding_status", "needs_revalidation"),
    ("grounding_status", "unsupported"),
    ("derivation", "synthesized"),
    ("question_state", "resolved"),
    ("question_state", "deferred"),
)

PLAIN_AXIS_VALUES = frozenset(
    {
        ("lifecycle_status", "active"),
        ("decision_state", ""),
        ("epistemic_status", "asserted"),
        ("epistemic_status", "not_applicable"),
        ("grounding_status", "grounded"),
        ("derivation", "explicit"),
        ("question_state", ""),
    }
)
"""Axis values that must render without a tag. Listing them turns a new status
value in the contract into a failure here instead of a claim that reads plain."""

MOVE_KINDS: Mapping[str, str] = {
    "rename": "页面改名；Claim 身份、版本与证据不变。",
    "merge": "页面合并；两侧 Claim 的身份、版本与证据都不变。",
    "split": "页面拆分；Claim 身份、版本与证据不变。",
    "reorganize": "页面重新归档；Claim 身份、版本与证据不变。",
}


def scope_columns(scope: Scope) -> dict[str, str]:
    """The scope columns every projection row is keyed by.

    A page is unique per (knowledge space, project, slug). A caller that named
    only the space would write a page another project can read, so the pair is
    produced here rather than spelled out at each call site.
    """

    return {"knowledge_space_id": scope.knowledge_space_id, "project_id": scope.project_id}


def claim_version_row(claim: Mapping[str, Any]) -> dict[str, Any]:
    """One claim version dict from either shape the store returns.

    `get_claim` answers with the version under `selected_version` plus its origins
    and relations; `iter_claims` answers with the version row itself. Accepting
    both is what keeps a caller from having to know which read produced the claim.
    """

    if not isinstance(claim, Mapping):
        raise ValueError("A claim must be a mapping of its committed fields.")
    if "selected_version" not in claim:
        return dict(claim)
    version = dict(claim["selected_version"] or {})
    if not version.get("claim_id"):
        version["claim_id"] = claim.get("claim_id")
    for key in ("origins", "relations"):
        if claim.get(key) is not None:
            version[key] = claim[key]
    return version


def claim_contract_state(claim: Mapping[str, Any]) -> ClaimState:
    """The status axes of one claim dict, checked by the frozen contract.

    A row whose axes the contract rejects is not rendered at all: a page that
    guessed at an illegal combination would be the one place a reader cannot tell
    that it had been guessed.
    """

    version = claim_version_row(claim)
    try:
        return ClaimState.from_dict(version)
    except KeyError as error:
        raise ValueError(f"claim is missing the contract field {error.args[0]!r}.")


def claim_contract_version(claim: Mapping[str, Any]) -> ClaimVersionState:
    """The full contract view of one claim dict, so a broken row raises here."""

    version = claim_version_row(claim)
    try:
        return ClaimVersionState(
            claim_version_id=version["claim_version_id"],
            claim_id=version["claim_id"],
            statement=version["statement"],
            state=claim_contract_state(version),
            conditions=tuple(str(item) for item in (version.get("conditions") or ())),
            attributes=version.get("attributes") or {},
        )
    except KeyError as error:
        raise ValueError(f"claim is missing the contract field {error.args[0]!r}.")


def labels_for_claim(claim: Mapping[str, Any]) -> list[str]:
    """The visible tags one claim's own axes earn, in a fixed order.

    A plain asserted, grounded, active claim carries none: a tag marks what a
    reader would otherwise have to assume, so the ordinary case is its absence.
    """

    version = claim_version_row(claim)
    claim_contract_state(version)
    for axis in (
        "lifecycle_status",
        "decision_state",
        "epistemic_status",
        "grounding_status",
        "derivation",
        "question_state",
    ):
        value = version.get(axis)
        key = (axis, "" if value is None else str(value))
        if key in LABELS or key in PLAIN_AXIS_VALUES:
            continue
        raise ValueError(f"{axis} value {value!r} has no label in renderer {RENDERER_VERSION}.")
    return [
        LABELS[(axis, value)]
        for axis, value in LABEL_ORDER
        if version.get(axis) is not None and str(version.get(axis)) == value
    ]


def current_claims(claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The newest committed version of each claim, with retracted claims dropped.

    An older version is not current knowledge and a retracted claim is not
    knowledge at all, so a template that answers a query carries neither.
    """

    newest: dict[str, dict[str, Any]] = {}
    for claim in claims or ():
        version = claim_version_row(claim)
        if str(version.get("lifecycle_status") or "") == "retracted":
            continue
        claim_id = str(version.get("claim_id") or "")
        if not claim_id:
            continue
        number = int(version.get("version") or 0)
        if claim_id not in newest or number > int(newest[claim_id].get("version") or 0):
            newest[claim_id] = version
    return [newest[claim_id] for claim_id in sorted(newest)]


def render_page(
    *,
    page_slug: str,
    title: str,
    claims: Sequence[Mapping[str, Any]],
    topics: Sequence[Any] = (),
    generated_at: str,
) -> dict[str, Any]:
    """Render one page from committed claim versions, and prove where it came from.

    `claims` accepts both shapes the store returns: a version row from
    `iter_claims`, or a `get_claim` detail whose version sits under
    `selected_version`. The detail carries origins and relations, which is what
    puts a synthesized paragraph's inference note and an attaching rationale's
    section on the page; a bare version row renders the same paragraph without the
    note it does not carry.
    """

    if not page_slug or not title:
        raise ValueError("A page needs a slug and a title.")
    versions: dict[str, dict[str, Any]] = {}
    for claim in claims or ():
        version = claim_version_row(claim)
        claim_contract_version(version)
        versions.setdefault(str(version["claim_version_id"]), version)

    placed = _place_versions(list(versions.values()))
    lines: list[str] = [f"# {title}", "", _metadata_line(page_slug, topics, generated_at), ""]
    entries: list[dict[str, Any]] = []
    for section in SECTIONS:
        section_versions = placed.get(section, [])
        if not section_versions:
            continue
        lines.append(f"## {section}")
        lines.append("")
        for version in section_versions:
            lines.extend(_paragraph(version))
            lines.append("")
            entries.append(
                {
                    "section": section,
                    "claim_id": str(version["claim_id"]),
                    "claim_version_id": str(version["claim_version_id"]),
                    "version": int(version.get("version") or 0),
                }
            )

    markdown = "\n".join(lines).rstrip("\n") + "\n"
    manifest = {
        "page_slug": page_slug,
        "title": title,
        "renderer_version": RENDERER_VERSION,
        "entries": entries,
        "claim_versions": sorted(versions),
    }
    return {
        "markdown": markdown,
        "manifest": manifest,
        "content_sha256": page_content_sha256(markdown),
        "projection_status": "current",
    }


def stale_projection_fallback(
    *,
    claims: Sequence[Mapping[str, Any]],
    page_slug: str,
    title: str,
) -> dict[str, Any]:
    """The conservative template a query falls back to when a projection is stale.

    It is the same renderer, so it cannot drift from the fresh page. What it drops
    is what a stale page cannot prove: only the newest committed version of each
    claim survives and retracted claims are gone, so the reader sees current
    knowledge under a status that says the stored page was not used.
    """

    kept = current_claims(claims)
    page = render_page(page_slug=page_slug, title=title, claims=kept, generated_at=_latest_committed_at(kept))
    return {**page, "projection_status": "stale", "fallback": "stale_projection_fallback"}


def page_content_sha256(markdown: str) -> str:
    """The hash that says whether the page on disk is still the page we wrote."""

    return "sha256:" + hashlib.sha256(str(markdown if markdown is not None else "").encode("utf-8")).hexdigest()


def detect_manual_edit(*, recorded_sha256: str | None, on_disk_text: str | None) -> dict[str, Any]:
    """Whether the page on disk still holds the bytes this renderer wrote.

    An edited page is never overwritten: the caller turns the added text into a
    manual_note source and re-ingests it. `action` names that hand-off, because
    "a human wrote here" is only useful together with what to do about it. A
    missing file is not an edit: there is nothing of the user's to preserve.
    """

    if on_disk_text is None:
        return {"edited": False, "action": "none"}
    if not recorded_sha256:
        return {"edited": False, "action": "none"}
    edited = page_content_sha256(on_disk_text) != recorded_sha256
    return {"edited": edited, "action": "manual_note" if edited else "none"}


def plan_rebuild(
    *,
    existing_pages: Sequence[Any],
    claims: Sequence[Mapping[str, Any]],
    requested_slugs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Which pages a claim change actually affects, so only those turn dirty.

    A page is affected when the newest version of a claim it covers is not one it
    recorded, when its renderer version moved, when it is already marked dirty, or
    when it has no manifest to prove it was ever rendered. A claim dict may carry
    `page_slug` or `page_slugs` from routing; a page is affected by those claims as
    well as by the claims its own manifest records.
    """

    live: dict[str, dict[str, Any]] = {}
    for claim in claims or ():
        version = claim_version_row(claim)
        claim_id = str(version.get("claim_id") or "")
        if not claim_id:
            continue
        number = int(version.get("version") or 0)
        if claim_id not in live or number > int(live[claim_id].get("version") or 0):
            live[claim_id] = version

    routed: dict[str, set[str]] = {}
    for claim_id, version in live.items():
        for slug in _routed_slugs(version):
            routed.setdefault(slug, set()).add(claim_id)

    rebuild: list[str] = []
    dirty: list[str] = []
    unchanged: list[str] = []
    recorded: set[str] = set()
    for page in existing_pages or ():
        slug, info = _page_record(page)
        if slug in recorded:
            continue
        recorded.add(slug)
        page_claim_ids = set(info["claim_ids"]) | routed.get(slug, set())
        needed = {live[claim_id]["claim_version_id"] for claim_id in page_claim_ids if claim_id in live}
        affected = (
            not needed.issubset(info["claim_version_ids"])
            or info["renderer_version"] not in ("", RENDERER_VERSION)
            or info["dirty"]
            or info["unproven"]
        )
        if affected:
            rebuild.append(slug)
            dirty.append(slug)
        else:
            unchanged.append(slug)

    for slug in requested_slugs or ():
        key = str(slug)
        if key not in rebuild:
            rebuild.append(key)
        if key in recorded and key not in dirty:
            dirty.append(key)
    for slug in sorted(routed):
        if slug not in recorded:
            rebuild.append(slug)

    return {
        "rebuild": sorted(set(rebuild)),
        "unchanged": sorted(unchanged),
        "dirty": sorted(set(dirty)),
    }


def plan_page_move(*, old_slug: str, new_slug: str, kind: str) -> dict[str, Any]:
    """The redirect record one page move writes, and nothing about the claims.

    The function takes no claim ids and returns none: a rename, merge, split or
    reorganise changes the mapping from claims to pages, never the identity of a
    claim, its evidence or its history.
    """

    if kind not in MOVE_KINDS:
        raise ValueError(f"Unknown page move kind: {kind!r}.")
    if not old_slug or not new_slug:
        raise ValueError("A page move needs both the old and the new slug.")
    return {
        "old_slug": str(old_slug),
        "new_slug": str(new_slug),
        "reason": MOVE_KINDS[kind],
        "status": "redirected",
    }


def resolve_page_slug(
    *,
    slug: Any,
    pages: Sequence[Any] = (),
    redirects: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Where a slug points, as a status a caller can read.

    An address that once worked must not answer like a page that never existed, so
    a moved slug resolves through its redirect record and anything unknown says
    `unknown` instead of returning an empty page.
    """

    key = str(slug or "")
    known = set()
    for page in pages or ():
        page_slug = page if isinstance(page, str) else _row_value(page, "slug", "page_slug")
        if page_slug:
            known.add(str(page_slug))
    for redirect in redirects or ():
        if not isinstance(redirect, Mapping):
            continue
        if str(redirect.get("old_slug") or "") != key:
            continue
        target = str(redirect.get("new_slug") or "")
        if target and target != key:
            return {
                "slug": key,
                "status": "redirected",
                "page_slug": target,
                "reason": str(redirect.get("reason") or ""),
            }
    if key in known:
        return {"slug": key, "status": "current", "page_slug": key, "reason": ""}
    return {
        "slug": key,
        "status": "unknown",
        "page_slug": None,
        "reason": "no page or redirect record names this slug",
    }


def _order_key(version: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(version.get("committed_at") or ""),
        str(version.get("claim_id") or ""),
        int(version.get("version") or 0),
        str(version.get("claim_version_id") or ""),
    )


def _place_versions(versions: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Every claim version in the one section it belongs to, in a fixed order.

    A claim whose lifecycle is no longer active, or which a later version has
    replaced, goes to 历史变化: rendering it beside current knowledge would let a
    reader take a retired statement for what the project holds today.
    """

    newest: dict[str, int] = {}
    for version in versions:
        claim_id = str(version.get("claim_id") or "")
        number = int(version.get("version") or 0)
        if claim_id not in newest or number > newest[claim_id]:
            newest[claim_id] = number

    kind_sections: dict[str, str] = {}
    for version in versions:
        kind = str(version.get("knowledge_kind") or "")
        section = SECTION_FOR_KIND.get(kind)
        if section is None and kind not in ATTACHING_KINDS:
            raise ValueError(f"Unknown knowledge kind: {kind!r}.")
        kind_sections[str(version["claim_version_id"])] = section or ""

    placed: dict[str, list[dict[str, Any]]] = {section: [] for section in SECTIONS}
    for version in sorted(versions, key=_order_key):
        section = kind_sections[str(version["claim_version_id"])] or _attached_section(version, kind_sections)
        if not _is_current(version, newest):
            section = HISTORY_SECTION
        placed[section].append(version)
    return {section: items for section, items in placed.items() if items}


def _is_current(version: Mapping[str, Any], newest: Mapping[str, int]) -> bool:
    lifecycle = str(version.get("lifecycle_status") or "active")
    if lifecycle != "active":
        return False
    claim_id = str(version.get("claim_id") or "")
    return int(version.get("version") or 0) >= newest.get(claim_id, 0)


def _attached_section(version: Mapping[str, Any], kind_sections: Mapping[str, str]) -> str:
    """The section of the claim a rationale or constraint attaches to."""

    candidates: list[tuple[int, str, str]] = []
    for relation_type, neighbour in _relation_neighbours(version):
        section = kind_sections.get(neighbour, "")
        if section:
            candidates.append((ATTACHMENT_PRIORITY.get(relation_type, len(ATTACHMENT_PRIORITY)), neighbour, section))
    if not candidates:
        return DEFAULT_SECTION
    return min(candidates)[2]


def _relation_neighbours(version: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every other claim version a live relation puts this one next to."""

    pairs: list[tuple[str, str]] = []
    self_version = str(version.get("claim_version_id") or "")
    for relation in version.get("relations") or ():
        if not isinstance(relation, Mapping):
            continue
        if str(relation.get("relation_status") or "") == "retracted":
            continue
        relation_type = str(relation.get("relation_type") or "")
        for key in ("from_claim_version_id", "to_claim_version_id"):
            neighbour = str(relation.get(key) or "")
            if neighbour and neighbour != self_version:
                pairs.append((relation_type, neighbour))
    return pairs


def _metadata_line(page_slug: str, topics: Sequence[Any], generated_at: str) -> str:
    parts = [f"页面：{page_slug}", f"渲染器：{RENDERER_VERSION}"]
    topic_labels = [label for label in (_topic_label(topic) for topic in topics or ()) if label]
    if topic_labels:
        parts.append("主题：" + "、".join(topic_labels))
    if generated_at:
        parts.append(f"生成时间：{generated_at}")
    return "> " + " · ".join(parts)


def _topic_label(topic: Any) -> str:
    if isinstance(topic, str):
        return topic
    if isinstance(topic, Mapping):
        return str(topic.get("canonical_label") or topic.get("label") or topic.get("topic_id") or "")
    return ""


def _paragraph(version: Mapping[str, Any]) -> list[str]:
    tags = "".join(labels_for_claim(version))
    lines = [f"**{version['statement']}**{tags}"]
    for condition in version.get("conditions") or ():
        lines.append(f"- 条件：{condition}")
    for note, assumptions in _derivation_notes(version):
        if note:
            lines.append(f"- 推导说明：{note}")
        for assumption in assumptions:
            lines.append(f"- 假设：{assumption}")
    attribution = _attribution_line(version)
    if attribution:
        lines.append(attribution)
    return lines


def _derivation_notes(version: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """The inference notes a synthesized claim recorded, in recorded order."""

    notes: list[tuple[str, list[str]]] = []
    for origin in version.get("origins") or ():
        if not isinstance(origin, Mapping):
            continue
        if str(origin.get("derivation") or "") != "synthesized":
            continue
        note = str(origin.get("inference_note") or "").strip()
        assumptions = [str(item) for item in origin.get("assumptions") or ()]
        if note or assumptions:
            notes.append((note, assumptions))
    if notes or str(version.get("derivation") or "") != "synthesized":
        return notes
    note = str(version.get("inference_note") or "").strip()
    assumptions = [str(item) for item in version.get("assumptions") or ()]
    return [(note, assumptions)] if note or assumptions else []


def _attribution_line(version: Mapping[str, Any]) -> str:
    """Who said it and when it was recorded, without ever filling in a time.

    A claim whose material never stated a time renders no time. The page shows
    what was recorded, and an undated claim stays undated on the page too.
    """

    parts: list[str] = []
    asserted_by = str(version.get("asserted_by") or "")
    if asserted_by and asserted_by != "unknown":
        parts.append(asserted_by)
    asserted_at = version.get("asserted_at")
    if asserted_at:
        parts.append(f"记录时间 {asserted_at}")
    return "- 归因：" + " · ".join(parts) if parts else ""


def _latest_committed_at(versions: Sequence[Mapping[str, Any]]) -> str:
    stamps = [str(version.get("committed_at") or "") for version in versions]
    return max(stamps) if stamps else ""


def _routed_slugs(version: Mapping[str, Any]) -> list[str]:
    slugs: list[str] = []
    single = version.get("page_slug")
    if single:
        slugs.append(str(single))
    for slug in version.get("page_slugs") or ():
        if slug:
            slugs.append(str(slug))
    return slugs


def _page_record(page: Any) -> tuple[str, dict[str, Any]]:
    """One projection row as the rebuild plan needs it, tolerating a bare slug."""

    if isinstance(page, str):
        return page, {
            "claim_ids": set(),
            "claim_version_ids": set(),
            "renderer_version": "",
            "dirty": False,
            "unproven": True,
        }
    if not isinstance(page, Mapping):
        raise ValueError("An existing page must be a row or a slug.")
    slug = str(page.get("slug") or page.get("page_slug") or "")
    if not slug:
        raise ValueError("An existing page needs a slug.")
    manifest = _manifest_of(page)
    entries = [entry for entry in manifest.get("entries") or () if isinstance(entry, Mapping)]
    claim_version_ids = {
        str(entry.get("claim_version_id")) for entry in entries if entry.get("claim_version_id")
    }
    claim_ids = {str(entry.get("claim_id")) for entry in entries if entry.get("claim_id")}
    claim_version_ids |= {str(value) for value in page.get("claim_version_ids") or () if value}
    claim_ids |= {str(value) for value in page.get("claim_ids") or () if value}
    renderer_version = str(manifest.get("renderer_version") or page.get("renderer_version") or "")
    status = str(page.get("projection_status") or "")
    return slug, {
        "claim_ids": claim_ids,
        "claim_version_ids": claim_version_ids,
        "renderer_version": renderer_version,
        "dirty": bool(page.get("dirty")) or status not in ("", "current"),
        "unproven": not claim_version_ids and not renderer_version,
    }


def _manifest_of(page: Mapping[str, Any]) -> dict[str, Any]:
    """A projection row's manifest, whether it arrived parsed or as stored JSON.

    The table keeps the manifest as JSON text, so a caller that hands over the row
    it read has not parsed it yet. Reading both shapes is what keeps the rebuild
    plan from treating a rendered page as one with no recorded inputs.
    """

    raw = page.get("manifest")
    if raw is None:
        raw = page.get("manifest_json")
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def _row_value(row: Any, *keys: str) -> str:
    if not isinstance(row, Mapping):
        return ""
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""
