"""Immutable parse snapshots and the exact source text a citation recovers.

Two rules carry this module. A parse snapshot never changes once frozen, and an
offset is counted in Unicode code points over the saved normalized text. Python
strings already index by code point, so `text[start:end]` is the whole answer;
reaching for byte or UTF-16 lengths is what this module exists to make
unnecessary.

Standard library only, and no sibling import by package name, so the same bytes
work as the canonical module and as the vendored copy inside the skill package.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

OFFSET_UNIT = "unicode_code_point"
RENDER_RECIPE_VERSION = "1"

RECIPE_VERBATIM = "verbatim"
RECIPE_TABLE_WITH_HEADER = "table_with_header"
RECIPE_CODE_WITH_FENCE = "code_with_fence"
RECIPE_CHARACTER_WINDOW = "character_window"
RECIPE_CONVERSATION_WITH_ROLE = "conversation_with_role"

RECIPES = (
    RECIPE_VERBATIM,
    RECIPE_TABLE_WITH_HEADER,
    RECIPE_CODE_WITH_FENCE,
    RECIPE_CHARACTER_WINDOW,
    RECIPE_CONVERSATION_WITH_ROLE,
)

_FOOTNOTE_PREFIXES = ("[^", "*", "†", "‡", "注", "Note:", "note:")


class EvidenceError(ValueError):
    """An unresolvable citation. The code says which kind of failure it was."""

    CODES = (
        "EVIDENCE_NOT_FOUND",
        "SCOPE_MISMATCH",
        "HASH_MISMATCH",
        "SOURCE_WITHDRAWN",
        "RAW_UNAVAILABLE",
        "INVALID_SPAN",
        "STRUCTURE_MISMATCH",
    )

    def __init__(self, message: str, *, code: str):
        if code not in self.CODES:
            raise ValueError(f"Unknown evidence error code: {code!r}")
        super().__init__(message)
        self.code = code


def normalize_text(raw: str) -> str:
    """The one transformation between received material and saved artifact text.

    Line endings become ``\\n`` so a CRLF file and its LF twin share one address
    space. Nothing else moves: combining characters, emoji, and full-width forms
    are left exactly as received, because folding them would make the recovered
    quote differ from the material a reader can go and check.
    """

    return raw.replace("\r\n", "\n").replace("\r", "\n")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest(*parts: Any) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(str(part).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


@dataclass(frozen=True)
class Source:
    """A material's stable identity, independent of its title or content."""

    source_id: str
    project_id: str
    source_type: str
    label: str
    origin_uri: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class SourceRevision:
    """One immutable capture of a source. New content is a new revision."""

    revision_id: str
    source_id: str
    raw_sha256: str
    captured_at: str
    raw_available: bool
    source_time: str | None = None
    storage_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "source_id": self.source_id,
            "raw_sha256": self.raw_sha256,
            "captured_at": self.captured_at,
            "raw_available": self.raw_available,
            "source_time": self.source_time,
            "storage_key": self.storage_key,
        }


@dataclass(frozen=True)
class Artifact:
    """A frozen parse output. Evidence points here, never at a live parser.

    ``structure`` is the block index the parser produced, saved alongside the
    text. Keeping it here is what lets a table citation recover its header and
    units years later, on a machine where that parser is no longer installed.
    """

    artifact_id: str
    revision_id: str
    normalized_text: str
    normalized_sha256: str
    parser_name: str
    parser_version: str
    config_hash: str
    structure: Mapping[str, Any] = field(default_factory=dict)
    parse_quality: str = "ok"

    def as_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "normalized_sha256": self.normalized_sha256,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "config_hash": self.config_hash,
            "structure": self.structure,
            "parse_quality": self.parse_quality,
        }
        if include_text:
            payload["normalized_text"] = self.normalized_text
        return payload


def freeze_artifact(
    *,
    revision_id: str,
    text: str,
    parser_name: str,
    parser_version: str,
    config_hash: str,
    structure: Mapping[str, Any] | None = None,
    parse_quality: str = "ok",
) -> Artifact:
    """Freeze one parse output under an address derived from its own content.

    Re-processing the same revision with the same parser and config produces the
    same artifact id, which is what makes a re-run idempotent instead of a second
    competing snapshot. A parser or config change moves the id, so old citations
    keep resolving against the old artifact.
    """

    normalized = normalize_text(text)
    digest = sha256_text(normalized)
    artifact_id = "art_" + _digest(
        revision_id, parser_name, parser_version, config_hash, digest
    )
    return Artifact(
        artifact_id=artifact_id,
        revision_id=revision_id,
        normalized_text=normalized,
        normalized_sha256=digest,
        parser_name=parser_name,
        parser_version=parser_version,
        config_hash=config_hash,
        structure=dict(structure or {}),
        parse_quality=parse_quality,
    )


def verify_artifact(artifact: Artifact) -> None:
    """Fail closed when the saved text no longer hashes to what was frozen."""

    actual = sha256_text(artifact.normalized_text)
    if actual != artifact.normalized_sha256:
        raise EvidenceError(
            f"Artifact {artifact.artifact_id} text does not match its recorded hash.",
            code="HASH_MISMATCH",
        )


def span_hash(text: str, start: int, end: int) -> str:
    return sha256_text(text[start:end])


def check_span(text: str, start: int, end: int) -> None:
    """Reject a span that does not fit, rather than clipping it to fit.

    Clipping would return a plausible-looking shorter quote, which is worse than
    an error because a reader cannot tell it happened.
    """

    if not isinstance(start, int) or not isinstance(end, int):
        raise EvidenceError("Span offsets must be integers.", code="INVALID_SPAN")
    if start < 0 or end <= start:
        raise EvidenceError(
            f"Span must satisfy 0 <= start < end; got [{start}, {end}).", code="INVALID_SPAN"
        )
    if end > len(text):
        raise EvidenceError(
            f"Span [{start}, {end}) runs past the end of the artifact ({len(text)} code points).",
            code="INVALID_SPAN",
        )


@dataclass(frozen=True)
class EvidenceRecord:
    """The stored form of one citation."""

    evidence_id: str
    project_id: str
    artifact_id: str
    spans: tuple[tuple[int, int], ...]
    span_hashes: tuple[str, ...]
    heading_path: tuple[str, ...] = ()
    structural_context_refs: tuple[int, ...] = ()
    offset_unit: str = OFFSET_UNIT
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "spans": [list(span) for span in self.spans],
            "span_hashes": list(self.span_hashes),
            "heading_path": list(self.heading_path),
            "structural_context_refs": list(self.structural_context_refs),
            "offset_unit": self.offset_unit,
            "label": self.label,
        }


def make_evidence(
    *,
    project_id: str,
    artifact: Artifact,
    spans: Sequence[tuple[int, int]],
    heading_path: Sequence[str] = (),
    structural_context_refs: Sequence[int] = (),
    label: str = "",
) -> EvidenceRecord:
    """Build a citation and hash every span against the artifact it names."""

    if not spans:
        raise EvidenceError("An evidence reference needs at least one span.", code="INVALID_SPAN")
    ordered = tuple((int(start), int(end)) for start, end in spans)
    hashes: list[str] = []
    for start, end in ordered:
        check_span(artifact.normalized_text, start, end)
        hashes.append(span_hash(artifact.normalized_text, start, end))
    evidence_id = "evd_" + _digest(
        project_id, artifact.artifact_id, ordered, hashes, tuple(heading_path)
    )
    return EvidenceRecord(
        evidence_id=evidence_id,
        project_id=project_id,
        artifact_id=artifact.artifact_id,
        spans=ordered,
        span_hashes=tuple(hashes),
        heading_path=tuple(heading_path),
        structural_context_refs=tuple(int(value) for value in structural_context_refs),
        label=label,
    )


@dataclass(frozen=True)
class RecoveredEvidence:
    """What a caller gets back when a citation resolves."""

    evidence_id: str
    project_id: str
    artifact_id: str
    exact_text: str
    segments: tuple[str, ...]
    span_hashes: tuple[str, ...]
    heading_path: tuple[str, ...]
    structural_context: tuple[str, ...]
    source_label: str
    raw_available: bool
    derived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "exact_text": self.exact_text,
            "segments": list(self.segments),
            "span_hashes": list(self.span_hashes),
            "heading_path": list(self.heading_path),
            "structural_context": list(self.structural_context),
            "source_label": self.source_label,
            "raw_available": self.raw_available,
            "derived": self.derived,
        }


def recover(
    *,
    record: EvidenceRecord,
    artifact: Artifact,
    expected_project_id: str,
    source_withdrawn: bool = False,
    raw_available: bool = True,
    source_label: str = "",
) -> RecoveredEvidence:
    """Resolve one stored citation to the exact text it names.

    Every failure here is a refusal to invent text. When the artifact hash, a
    span hash, or the project scope does not line up, the caller gets an error
    naming which one, and no text at all.
    """

    if record.project_id != expected_project_id:
        raise EvidenceError(
            f"Evidence {record.evidence_id} belongs to project {record.project_id}, not {expected_project_id}.",
            code="SCOPE_MISMATCH",
        )
    if source_withdrawn:
        raise EvidenceError(
            f"Evidence {record.evidence_id} names a withdrawn source.", code="SOURCE_WITHDRAWN"
        )
    if record.artifact_id != artifact.artifact_id:
        raise EvidenceError(
            f"Evidence {record.evidence_id} names artifact {record.artifact_id}, not {artifact.artifact_id}.",
            code="EVIDENCE_NOT_FOUND",
        )
    verify_artifact(artifact)

    segments: list[str] = []
    for (start, end), recorded in zip(record.spans, record.span_hashes):
        check_span(artifact.normalized_text, start, end)
        actual = span_hash(artifact.normalized_text, start, end)
        if actual != recorded:
            raise EvidenceError(
                f"Span [{start}, {end}) of {artifact.artifact_id} does not match its recorded hash.",
                code="HASH_MISMATCH",
            )
        segments.append(artifact.normalized_text[start:end])

    return RecoveredEvidence(
        evidence_id=record.evidence_id,
        project_id=record.project_id,
        artifact_id=record.artifact_id,
        exact_text="\n".join(segments),
        segments=tuple(segments),
        span_hashes=tuple(record.span_hashes),
        heading_path=tuple(record.heading_path),
        structural_context=structural_context(artifact, record.structural_context_refs),
        source_label=source_label,
        raw_available=raw_available,
    )


def structural_context(artifact: Artifact, refs: Sequence[int]) -> tuple[str, ...]:
    """The scaffolds a citation needs to be readable, recovered from the snapshot.

    A table's header row and units, a code block's real definition, a
    conversation's previous turn: the parts a reader needs and the citation does
    not itself cover. These come out of the frozen structure, so no parser runs.
    """

    blocks = list(artifact.structure.get("blocks", []))
    lines = list(artifact.structure.get("lines", []))
    text = artifact.normalized_text
    context: list[str] = []
    for index in refs:
        if not isinstance(index, int) or index < 0 or index >= len(blocks):
            raise EvidenceError(
                f"Structural context index {index} is outside the frozen structure.",
                code="STRUCTURE_MISMATCH",
            )
        block = blocks[index]
        for span in block.get("context_spans", []):
            start, end = int(span[0]), int(span[1])
            check_span(text, start, end)
            context.append(text[start:end])
        if block.get("kind") == "table":
            for line_index in block.get("footnote_lines", []):
                if 0 <= int(line_index) < len(lines):
                    start, end = lines[int(line_index)]
                    context.append(text[int(start):int(end)])
    return tuple(context)


def line_spans(text: str) -> list[tuple[int, int]]:
    """Newline-delimited line ranges, expressed in code points."""

    spans: list[tuple[int, int]] = []
    position = 0
    total = len(text)
    while position < total:
        newline = text.find("\n", position)
        if newline == -1:
            spans.append((position, total))
            break
        spans.append((position, newline))
        position = newline + 1
    return spans


def build_structure(text: str, blocks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The saved block index: every block's own spans plus what it scaffolds."""

    return {
        "offset_unit": OFFSET_UNIT,
        "line_count": len(line_spans(text)),
        "lines": [list(span) for span in line_spans(text)],
        "blocks": [dict(block) for block in blocks],
    }


def footnote_lines(text: str, lines: Sequence[tuple[int, int]], start_index: int) -> list[int]:
    """Table footnotes: the contiguous marked lines directly under a table."""

    found: list[int] = []
    index = start_index
    while index < len(lines):
        start, end = lines[index]
        stripped = text[start:end].strip()
        if not stripped:
            break
        if not stripped.startswith(_FOOTNOTE_PREFIXES):
            break
        found.append(index)
        index += 1
    return found


@dataclass(frozen=True)
class RenderedChunk:
    """Model-facing text, with the recipe that produced it.

    `evidence_spans` are real ranges in the artifact. `context_spans` are ranges
    the renderer re-emitted to make the piece readable. The two are kept apart so
    a fabricated fence can never be cited as if the source contained it.
    """

    chunk_id: str
    artifact_id: str
    index: int
    evidence_spans: tuple[tuple[int, int], ...]
    context_spans: tuple[tuple[int, int], ...]
    render_recipe: str
    text: str
    verbatim: bool
    heading_path: tuple[str, ...] = ()
    starting_line: int = 1

    def __post_init__(self) -> None:
        if self.render_recipe not in RECIPES:
            raise EvidenceError(f"Unknown render recipe: {self.render_recipe!r}.", code="STRUCTURE_MISMATCH")
        if self.verbatim and self.render_recipe != RECIPE_VERBATIM:
            raise EvidenceError(
                "A verbatim chunk must use the verbatim recipe.", code="STRUCTURE_MISMATCH"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "artifact_id": self.artifact_id,
            "index": self.index,
            "evidence_spans": [list(span) for span in self.evidence_spans],
            "context_spans": [list(span) for span in self.context_spans],
            "render_recipe": self.render_recipe,
            "render_recipe_version": RENDER_RECIPE_VERSION,
            "text": self.text,
            "verbatim": self.verbatim,
            "heading_path": list(self.heading_path),
            "starting_line": self.starting_line,
        }


def make_chunk_id(artifact_id: str, index: int, recipe: str, spans: Sequence[tuple[int, int]]) -> str:
    return "chk_" + _digest(artifact_id, index, recipe, tuple(spans))


def recipe_context_spans(
    text: str,
    evidence_spans: Sequence[tuple[int, int]],
    context_spans: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Keep only the context spans that are not already part of the evidence.

    A table header that the evidence covers needs no separate mention, and
    listing it twice would make a reader think two lines were re-emitted.
    """

    evidence = {(int(start), int(end)) for start, end in evidence_spans}
    kept: list[tuple[int, int]] = []
    for span in context_spans:
        pair = (int(span[0]), int(span[1]))
        check_span(text, *pair)
        if pair not in evidence:
            kept.append(pair)
    return tuple(kept)


def structure_json(structure: Mapping[str, Any]) -> str:
    return json.dumps(structure, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
