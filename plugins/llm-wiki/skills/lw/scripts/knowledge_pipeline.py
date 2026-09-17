"""The pure half of knowledge extraction: prepare, extract, validate.

Three questions, three functions. `prepare_ingest` freezes material into artifacts,
spans and batches. `extract_claims` asks the injected model roles what the batches
state, grounds every answer against the text it cites, and dispositions it.
`validate_changes` is the last gate: it re-reads each candidate against the material
and refuses the ones the material does not carry, so nothing reaches a change set on
the strength of a fluent sentence.

Nothing here reads a file, a database, or a socket. The model is injected, the history
is injected, and every id a caller sees is minted here, from the content it names.
That is what lets the same bytes run as a server module and as the vendored copy inside
the skill package, and what makes a dry run a real rehearsal of the commit.

Standard library only, and no sibling import by package name.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # pragma: no cover - the flat layout is the vendored skill copy
    from . import chunking, evidence, wiki_prompts
    from .knowledge_types import (
        EVIDENCE_ID_PATTERN,
        RUN_STATES,
        VALUE_GATE_DECISIONS,
        VALUE_GATE_REASON_CODES,
        Attribution,
        ClaimCandidate,
        ClaimState,
        KnowledgeError,
        Scope,
        build_change_set,
        validate_attributes,
    )
except ImportError:  # pragma: no cover - exercised by the flat layout only
    import chunking
    import evidence
    import wiki_prompts
    from knowledge_types import (
        EVIDENCE_ID_PATTERN,
        RUN_STATES,
        VALUE_GATE_DECISIONS,
        VALUE_GATE_REASON_CODES,
        Attribution,
        ClaimCandidate,
        ClaimState,
        KnowledgeError,
        Scope,
        build_change_set,
        validate_attributes,
    )

ModelFn = Callable[[dict[str, Any], str, list[dict[str, Any]]], dict[str, Any]]

DEFAULT_TARGET_CHARS = chunking.TARGET_CHUNK_CHARS
DEFAULT_MAX_CHARS = chunking.MAX_CHUNK_CHARS
DEFAULT_BATCH_BUDGET_CHARS = 48_000
PARSER_NAME = chunking.PARSER_NAME
PARSER_VERSION = chunking.PARSER_VERSION

MIN_STATEMENT_COVERAGE = 0.5
"""How much of a statement's own wording the cited material must carry.

An explicit claim says the material recorded this sentence. Half of the sentence's
distinctive terms being absent from the text it cites means the text is not where it
came from, and the sentence is a quotation of nothing.
"""

NEGATION_WINDOW = 32
"""Code points around a term within which a negation marker counts as negating it."""

QUOTE_ALLOWED_BASES = ("recorded",)

ARTIFACT_FIELDS = (
    "artifact_id",
    "revision_id",
    "normalized_text",
    "normalized_sha256",
    "parser_name",
    "parser_version",
    "config_hash",
    "structure",
    "parse_quality",
)


class RejectionCode:
    """Why a candidate never reaches a change set."""

    FABRICATED_ADOPTION = "fabricated_adoption"
    FABRICATED_VERIFICATION = "fabricated_verification"
    UNSUPPORTED_ASSERTION = "unsupported_assertion"
    CROSS_PROJECT_REFERENCE = "cross_project_reference"
    RECONSTRUCTED_REASON_AS_QUOTE = "reconstructed_reason_as_quote"
    MISSING_QUALIFIER = "missing_qualifier"
    CONTRADICTED_BY_EVIDENCE = "contradicted_by_evidence"
    INSTRUCTION_FROM_MATERIAL = "instruction_from_material"
    FORGED_IDENTIFIER = "forged_identifier"
    INVALID_CANDIDATE = "invalid_candidate"


ZERO_TOLERANCE_CODES = (
    RejectionCode.FABRICATED_ADOPTION,
    RejectionCode.FABRICATED_VERIFICATION,
    RejectionCode.UNSUPPORTED_ASSERTION,
    RejectionCode.CROSS_PROJECT_REFERENCE,
    RejectionCode.RECONSTRUCTED_REASON_AS_QUOTE,
    RejectionCode.MISSING_QUALIFIER,
    RejectionCode.CONTRADICTED_BY_EVIDENCE,
    RejectionCode.INSTRUCTION_FROM_MATERIAL,
)
"""The refusals the spec names as zero tolerance, in severity order. A caller that gates a
release counts these. `forged_identifier` and `invalid_candidate` are refusals as well, and
they are listed separately only because this tuple is the named set."""

REJECTION_ORDER = (
    RejectionCode.INSTRUCTION_FROM_MATERIAL,
    RejectionCode.FORGED_IDENTIFIER,
    RejectionCode.FABRICATED_ADOPTION,
    RejectionCode.FABRICATED_VERIFICATION,
    RejectionCode.CROSS_PROJECT_REFERENCE,
    RejectionCode.RECONSTRUCTED_REASON_AS_QUOTE,
    RejectionCode.MISSING_QUALIFIER,
    RejectionCode.CONTRADICTED_BY_EVIDENCE,
    RejectionCode.INVALID_CANDIDATE,
    RejectionCode.UNSUPPORTED_ASSERTION,
)
"""Which reason a rejection leads with when a candidate has several.

Obeying an instruction outranks everything, then a model writing identity or a status it
does not own, then the material-fidelity faults. `unsupported_assertion` is the catch-all
and is reported last, so a reader sees the specific fault when there is one."""


@dataclass(frozen=True)
class ModelRoles:
    """The four questions the pipeline asks a model, each injectable and optional.

    Every callable takes the same three arguments as `core.Model`, so one adapter serves
    the routed submit and the knowledge pipeline without a second signature. A role that
    is None means that stage runs on the pipeline's own deterministic rules and the batch
    records that no model answered.
    """

    discovery: ModelFn | None = None
    reasoning: ModelFn | None = None
    grounding: ModelFn | None = None
    render: ModelFn | None = None

    @classmethod
    def from_mapping(cls, mapping: Any) -> "ModelRoles":
        if isinstance(mapping, cls):
            return mapping
        if not isinstance(mapping, Mapping):
            raise KnowledgeError(
                f"Model roles must be a mapping or ModelRoles; got {type(mapping).__name__}.",
                code="INVALID_ROLES",
            )
        fields = ("discovery", "reasoning", "grounding", "render")
        aliases = {"model", "single", "default"}
        selected: dict[str, Any] = {}
        for key, value in mapping.items():
            name = str(key)
            if name in fields:
                selected[name] = value
            elif name in aliases:
                selected = {field: value for field in fields}
            else:
                raise KnowledgeError(
                    f"Unknown model role {name!r}; expected one of {', '.join(fields)}.",
                    code="INVALID_ROLES",
                )
        for name, value in selected.items():
            if value is not None and not callable(value):
                raise KnowledgeError(f"Model role {name!r} must be callable.", code="INVALID_ROLES")
        return cls(**selected)

    @classmethod
    def single(cls, fn: ModelFn | None) -> "ModelRoles":
        return cls(discovery=fn, reasoning=fn, grounding=fn, render=fn)

    def callable_for(self, role: str) -> ModelFn | None:
        """The callable that answers a role, which is not always a field of that name.

        Synthesis, value and identity are judgements over material the pipeline has
        already read, so they ride on the reasoning role. The role names belong to the
        prompt contract and this table is the one place the two vocabularies meet.
        """

        field = ROLE_FIELDS.get(role)
        return getattr(self, field) if field else None


ROLE_FIELDS: dict[str, str] = {
    wiki_prompts.ROLE_DISCOVERY: "discovery",
    wiki_prompts.ROLE_SYNTHESIS: "reasoning",
    wiki_prompts.ROLE_GROUNDING: "grounding",
    wiki_prompts.ROLE_VALUE: "reasoning",
    wiki_prompts.ROLE_IDENTITY: "reasoning",
}

SUSPICION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "rule_override",
        re.compile(
            r"(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|the\s+|these\s+)?"
            r"(?:previous|prior|earlier|above|system|safety)\s+"
            r"(?:rules?|instructions?|prompts?|guidelines?)"
            r"|(?:忽略|无视|忘记|绕过)(?:之前|以前|以上|所有|系统|全部|一切)*的?(?:规则|指令|提示|约束)",
            re.IGNORECASE,
        ),
    ),
    (
        "identity_escalation",
        re.compile(
            r"(?:set|mark|treat|consider|make|record)\s+(?:me|us|this|it|them)\b[^.\n]{0,40}"
            r"\b(?:approved|adopted|verified|authorized|authoritative|admin|canonical)\b"
            r"|(?:把我|将我|把它|将其|将该|把此)[^。\n]{0,12}"
            r"(?:设为|标记为|视为|认定为|当作)(?:已批准|已通过|已采用|已核实|已确认|权威|管理员|决定)"
            r"(?:的)?(?:决定|状态|条目|身份|结论)?",
            re.IGNORECASE,
        ),
    ),
    (
        "sensitive_path",
        re.compile(
            r"^[^\n]*\b(?:read|cat|open|load|fetch|send|upload|exfiltrate|paste|dump|print|读取|读|打开|"
            r"上传|发送)\b[^\n]*(?:/etc/|/root/|id_rsa|\.ssh/|\.env\b|passwd\b|shadow\b|secret|"
            r"credential|api[_-]?key|private[_-]?key|C:\\)[^\n]*$",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+(?:to\s+be|you\s+are)\b|"
            r"\bnew\s+instructions?\b|\bdeveloper\s+mode\b|从现在起你是|你现在是",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt",
        re.compile(
            r"(?:reveal|print|show|repeat|output|泄露|打印|输出)[^\n]{0,20}"
            r"(?:system\s+prompt|your\s+instructions?|系统提示|系统指令|你的指令)",
            re.IGNORECASE,
        ),
    ),
)
"""Instruction shapes a material may contain. Every one of them is data about the
material and never a command: the scan is read only, it can only add a note to a batch
record, and no candidate's status is computed from what it finds."""


def screen_material(text: str) -> list[str]:
    """The spans of `text` that read like instructions to the model.

    Returns the matched text in the order it appears, duplicates collapsed. The caller
    records them. Nothing is executed, nothing is followed, and no input raises, not even
    input that is not a string.
    """

    if not isinstance(text, str) or not text:
        return []
    found: list[tuple[int, str]] = []
    for _kind, pattern in SUSPICION_PATTERNS:
        for match in pattern.finditer(text):
            span = match.group(0).strip()
            if span:
                found.append((match.start(), span))
    ordered: list[str] = []
    for _position, span in sorted(found, key=lambda item: item[0]):
        if span not in ordered:
            ordered.append(span)
    return ordered


def screen_materials(materials: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every instruction-shaped span in a batch, with the chunk it came from."""

    found: list[dict[str, Any]] = []
    for material in materials:
        for span in screen_material(str(material.get("text", ""))):
            found.append({
                "chunk_id": str(material.get("chunk_id", "")),
                "evidence_id": str(material.get("evidence_id", "")),
                "span": span,
            })
    return found


def prepare_ingest(
    *,
    scope: Scope,
    source_inputs: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the received material and split it into model-sized batches.

    A source that does not fit one batch becomes more batches: its chunks are grouped
    against the batch budget, and a chunk larger than the budget is re-chunked smaller
    rather than cut. Coverage records every input source and the batch ids that carry it,
    so a source that produced no batch is visible instead of absent.
    """

    scope = _coerce_scope(scope)
    resolved = _resolve_config(config)
    fingerprint = resolved["config_fingerprint"]

    sources: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    records: dict[str, Any] = {}
    materials: list[dict[str, Any]] = []

    for position, raw in enumerate(source_inputs, start=1):
        source = _normalise_source_input(raw, position)
        text = evidence.normalize_text(source["content"])
        revision_id = _mint(
            "rev_",
            scope.knowledge_space_id,
            scope.project_id,
            source["source_id"],
            evidence.sha256_text(text),
        )
        artifact = evidence.freeze_artifact(
            revision_id=revision_id,
            text=text,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            config_hash=fingerprint,
            structure=chunking.artifact_structure(text),
        )
        chunks = chunking.chunk_text(
            artifact.normalized_text,
            target_chars=resolved["target_chars"],
            max_chars=resolved["max_chars"],
        )
        if any(len(chunk.text) > resolved["batch_budget_chars"] for chunk in chunks):
            chunks = chunking.chunk_text(
                artifact.normalized_text,
                target_chars=min(resolved["target_chars"], resolved["batch_budget_chars"]),
                max_chars=resolved["batch_budget_chars"],
            )

        source_materials: list[dict[str, Any]] = []
        for chunk in chunks:
            record = _freeze_chunk(
                scope=scope,
                artifact=artifact,
                chunk=chunk,
                label=source["label"],
                evidence_id=None,
            )
            records[record.evidence_id] = record
            material = _material(artifact, chunk, record.evidence_id, source["source_id"])
            source_materials.append(material)
            materials.append(material)

        artifacts[artifact.artifact_id] = artifact
        sources.append({
            "source_id": source["source_id"],
            "kind": source["kind"],
            "label": source["label"],
            "artifact_id": artifact.artifact_id,
            "revision_id": revision_id,
            "normalized_sha256": artifact.normalized_sha256,
            "chars": len(artifact.normalized_text),
            "chunk_ids": [material["chunk_id"] for material in source_materials],
            "evidence_ids": [material["evidence_id"] for material in source_materials],
        })

    batches = plan_batches(materials, budget=resolved["batch_budget_chars"])
    covered: dict[str, list[str]] = {source["source_id"]: [] for source in sources}
    for batch in batches:
        for source_id in batch["source_ids"]:
            covered[source_id].append(batch["batch_id"])

    run_id = _mint(
        "run_",
        scope.knowledge_space_id,
        scope.project_id,
        fingerprint,
        [(source["source_id"], source["normalized_sha256"]) for source in sources],
        [batch["batch_id"] for batch in batches],
    )
    purpose = str(resolved["extra"].get("purpose", "")).strip() or (
        f"Extract knowledge from {len(sources)} source(s) for project {scope.project_id}."
    )
    return {
        "run_id": run_id,
        "scope": scope.as_dict(),
        "purpose": purpose,
        "sources": sources,
        "batches": batches,
        "coverage": {
            "source_ids": [source["source_id"] for source in sources],
            "by_source": covered,
            "uncovered": [source_id for source_id, batch_ids in covered.items() if not batch_ids],
            "chunk_count": len(materials),
            "batch_count": len(batches),
        },
        "base_version": resolved["base_version"],
        "config": resolved,
        "artifacts": artifacts,
        "evidence": records,
        "config_fingerprint": fingerprint,
    }


def plan_batches(
    materials: Sequence[Mapping[str, Any]],
    *,
    budget: int = DEFAULT_BATCH_BUDGET_CHARS,
) -> list[dict[str, Any]]:
    """Group rendered chunks into batches that each fit the budget.

    The grouping is greedy and order preserving, so a batch is a contiguous run of the
    material and the last batch always carries the end of the document. A chunk larger
    than the budget raises here, because truncating it is exactly the silent loss this
    module exists to prevent.
    """

    if budget < 1:
        raise KnowledgeError("The batch budget must be at least one character.", code="INVALID_BATCH")
    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    used = 0

    def flush() -> None:
        nonlocal used
        if not current:
            return
        index = len(batches) + 1
        batches.append({
            "batch_id": f"batch-{index:03d}",
            "index": index,
            "materials": list(current),
            "chunk_ids": [material["chunk_id"] for material in current],
            "evidence_ids": [material["evidence_id"] for material in current],
            "source_ids": sorted({str(material.get("source_id", "")) for material in current}),
            "chars": used,
        })
        current.clear()
        used = 0

    for material in materials:
        size = len(str(material.get("text", "")))
        if size > budget:
            raise KnowledgeError(
                f"Chunk {material.get('chunk_id')!r} is {size} characters, over the {budget} character "
                "batch budget; it must be split into more batches rather than truncated.",
                code="INVALID_BATCH",
            )
        if current and used + size > budget:
            flush()
        current.append(dict(material))
        used += size
    flush()
    return batches


def material_snapshot(
    prepared_run: Mapping[str, Any],
    *,
    claims: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
    pages: Iterable[Any] = (),
    withdrawn: Iterable[str] = (),
    adoption_records: Mapping[str, Any] | None = None,
    verification_records: Mapping[str, Any] | None = None,
    dispute_records: Mapping[str, Any] | None = None,
    resolution_records: Mapping[str, Any] | None = None,
    base_version: int | None = None,
) -> dict[str, Any]:
    """The read-only view `validate_changes` checks a candidate against.

    It carries the frozen artifacts, the evidence records, and whatever the caller knows
    about committed claims and proof records. A candidate can only cite what is in here,
    which is what keeps project context, remembered text and an old page from quietly
    becoming evidence.
    """

    run = _require_run(prepared_run)
    return build_snapshot(
        scope=_coerce_scope(run["scope"]),
        run_id=str(run["run_id"]),
        artifacts=dict(run.get("artifacts") or {}),
        evidence_records=dict(run.get("evidence") or {}),
        claims=claims,
        pages=pages,
        withdrawn=withdrawn,
        base_version=int(run.get("base_version", 0) if base_version is None else base_version),
        adoption_records=adoption_records,
        verification_records=verification_records,
        dispute_records=dispute_records,
        resolution_records=resolution_records,
    )


def build_snapshot(
    *,
    scope: Scope,
    run_id: str,
    artifacts: Mapping[str, Any],
    evidence_records: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
    pages: Iterable[Any] = (),
    withdrawn: Iterable[str] = (),
    base_version: int = 0,
    adoption_records: Mapping[str, Any] | None = None,
    verification_records: Mapping[str, Any] | None = None,
    dispute_records: Mapping[str, Any] | None = None,
    resolution_records: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The same view as `material_snapshot`, for a caller that has no prepared run."""

    known: dict[str, dict[str, Any]] = {}
    if isinstance(claims, Mapping):
        items: Iterable[Any] = [
            {**dict(claim), "claim_version_id": key} for key, claim in claims.items()
        ]
    else:
        items = list(claims)
    for claim in items:
        data = dict(claim)
        version_id = str(data.get("claim_version_id") or data.get("id") or "")
        if version_id:
            known[version_id] = data
    return {
        "scope": scope.as_dict(),
        "run_id": str(run_id),
        "base_version": int(base_version),
        "artifacts": dict(artifacts),
        "evidence": dict(evidence_records),
        "claims": known,
        "known_claim_version_ids": sorted(known),
        "pages": [
            dict(page) if isinstance(page, Mapping) else str(page) for page in pages
        ],
        "withdrawn": sorted({str(value) for value in withdrawn}),
        "adoption_records": dict(adoption_records or {}),
        "verification_records": dict(verification_records or {}),
        "dispute_records": dict(dispute_records or {}),
        "resolution_records": dict(resolution_records or {}),
    }


def extract_claims(
    *,
    prepared_run: Mapping[str, Any],
    history_reader: Callable[[Scope, ClaimCandidate], Sequence[Mapping[str, Any]]],
    model_roles: ModelRoles | Mapping[str, Any],
    project_context: Iterable[Any] = (),
) -> dict[str, Any]:
    """Ask the model what each batch states, then ground and disposition every answer.

    A batch whose discovery answer cannot be read lands in `unfinished` and the run stays
    in `extracting`, so a failure is retryable and never reads as a completed extraction.
    Grounding and value never fail a batch: they can lower a disposition, they cannot
    invent one, and the material-level refusals are decided later, by `validate_changes`.
    """

    run = _require_run(prepared_run)
    result = _extract_batches(
        scope=_coerce_scope(run["scope"]),
        batches=list(run.get("batches") or ()),
        purpose=str(run.get("purpose") or ""),
        roles=ModelRoles.from_mapping(model_roles),
        history_reader=history_reader,
        project_context=tuple(project_context),
    )
    result["run_id"] = str(run["run_id"])
    result["status"] = "extracting" if result["unfinished"] else "validating"
    return result


def artifact_extractor(
    roles: ModelRoles | Mapping[str, Any] | None,
) -> Callable[[Mapping[str, Any]], list[dict[str, Any]]]:
    """Adapt the batch orchestrator to the per-artifact extractor the service calls.

    The service walks one frozen artifact at a time and already holds its chunks and the
    evidence ids it registered for them, so this adapter builds batches from that context,
    runs the same stages through the same code, and returns the claims `validate_changes`
    publishes. A candidate whose qualifier or modality did not survive the round trip is
    refused by that shared validation and is absent from the result.
    """

    model_roles = ModelRoles.from_mapping({} if roles is None else roles)

    def extract(context: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(context, Mapping):
            raise KnowledgeError("An extraction context object is required.", code="INVALID_CONTEXT")
        scope = _coerce_scope(context.get("scope"))
        artifact = _coerce_artifact(context.get("artifact"))
        chunks = list(context.get("chunks") or ())
        provided = {str(key): str(value) for key, value in dict(context.get("evidence_ids") or {}).items()}
        source_id = str(context.get("source_id") or "")
        purpose = str(context.get("purpose") or "")
        project_context = tuple(context.get("project_context") or ())
        history_reader = context.get("history_reader")

        def read_history(_scope: Scope, candidate: ClaimCandidate) -> list[dict[str, Any]]:
            if history_reader is None:
                return []
            return list(history_reader(candidate_payload(candidate)) or ())

        materials: list[dict[str, Any]] = []
        records: dict[str, Any] = {}
        for chunk in chunks:
            chunk_id = str(getattr(chunk, "chunk_id", ""))
            registered = provided.get(chunk_id)
            if not registered:
                raise KnowledgeError(
                    f"Chunk {chunk_id!r} has no registered evidence id; the artifact extractor cites only "
                    "evidence the store already holds.",
                    code="INVALID_CONTEXT",
                )
            alias = registered
            record = _freeze_chunk(
                scope=scope,
                artifact=artifact,
                chunk=chunk,
                label=source_id,
                evidence_id=registered,
            )
            records[alias] = record
            records[registered] = record
            materials.append(_material(artifact, chunk, alias, source_id))

        if not materials:
            return []
        batches = plan_batches(materials)
        result = _extract_batches(
            scope=scope,
            batches=batches,
            purpose=purpose,
            roles=model_roles,
            history_reader=read_history,
            project_context=project_context,
        )
        run_id = _mint(
            "run_",
            scope.knowledge_space_id,
            scope.project_id,
            artifact.artifact_id,
            str(context.get("base_version", 0)),
            [batch["batch_id"] for batch in batches],
        )
        snapshot = build_snapshot(
            scope=scope,
            run_id=run_id,
            artifacts={artifact.artifact_id: artifact},
            evidence_records=records,
            claims=list(context.get("claims") or ()),
            base_version=int(context.get("base_version", 0) or 0),
        )
        outcome = validate_changes(candidate_batch=result["candidates"], snapshot=snapshot)
        return list(outcome["changeset"]["claims"])

    return extract


def candidate_payload(candidate: Any) -> dict[str, Any]:
    """One candidate as plain data, for a caller that stores it or answers about it."""

    if isinstance(candidate, ClaimCandidate):
        return asdict(candidate)
    if isinstance(candidate, Mapping):
        return dict(candidate)
    if candidate is not None and hasattr(candidate, "statement"):
        return dict(asdict(candidate))
    raise KnowledgeError(
        f"A candidate is required; got {type(candidate).__name__}.",
        code="INVALID_CANDIDATE",
    )


def stage_provenance(notes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The per-stage model provenance a cost report reads.

    Each entry names the role, the model that answered, the variable that chose
    it, the prompt version, the attempt count and the provider-reported tokens.
    A provider that reports no usage yields `unknown` here, never an estimate.
    """

    return [dict(note[PROVENANCE_KEY]) for note in notes if PROVENANCE_KEY in note]


def run_status(result: Mapping[str, Any]) -> str:
    """The one run state a result supports, and never `completed` with work outstanding.

    A result that names an unfinished batch is still extracting whatever its status field
    says, because the alternative is a failed extraction that reads as a finished one.
    """

    if not isinstance(result, Mapping):
        raise KnowledgeError("A result object is required.", code="INVALID_RESULT")
    unfinished = [str(name) for name in result.get("unfinished") or ()]
    status = str(result.get("status") or "")
    if unfinished:
        return "failed" if status == "failed" else "extracting"
    if status in RUN_STATES:
        return status
    if "batches" in result or "candidates" in result:
        return "validating"
    return "received"


def explanation(claim: Any) -> dict[str, Any]:
    """How a claim's wording came to exist, and whether it may be shown as a quotation.

    A reconstructed explanation is system derived: it is the shape the material implies,
    not a sentence the material contains, so it has no quote to offer and the label
    travels with the text. Callers ask this question here rather than guessing from the
    statement, which is what keeps a reconstruction from being rendered as a quote.
    """

    if not isinstance(claim, Mapping):
        raise KnowledgeError("A claim object is required.", code="INVALID_CLAIM")
    state = claim.get("state") or {}
    if not isinstance(state, Mapping):
        state = {}
    derivation = str(state.get("derivation") or claim.get("derivation") or "explicit")
    basis = str(claim.get("explanation_basis") or "")
    if basis not in ("recorded", "reconstructed"):
        basis = "recorded" if derivation == "explicit" else "reconstructed"
    origins = [item for item in claim.get("origins") or () if isinstance(item, Mapping)]
    origin = origins[0] if origins else {}
    evidence_refs = list(claim.get("evidence_refs") or origin.get("evidence_refs") or ())
    return {
        "basis": basis,
        "label": "recorded" if basis == "recorded" else "system_derived_reconstruction",
        "derivation": derivation,
        "statement": str(claim.get("statement") or ""),
        "inference_note": str(origin.get("inference_note") or claim.get("inference_note") or ""),
        "evidence_refs": evidence_refs,
        "quote_allowed": basis in QUOTE_ALLOWED_BASES and bool(evidence_refs),
    }


def validate_changes(
    *,
    candidate_batch: Any,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """The last gate: re-read every candidate against the text it cites.

    Nothing is published until it has been re-read here. A candidate that fails is
    reported with a reason code and does not reach the change set. A candidate nothing
    failed and nothing dispositioned is an error rather than a silent pass, because
    "we did not decide" and "we decided to keep" are different facts and only one of
    them is safe to publish.
    """

    if not isinstance(snapshot, Mapping):
        raise KnowledgeError("A snapshot object is required.", code="INVALID_SNAPSHOT")
    scope = _coerce_scope(snapshot.get("scope"))
    run_id = str(snapshot.get("run_id") or "")
    if not run_id:
        raise KnowledgeError(
            "The snapshot carries no run_id; a change set must name the run that produced it.",
            code="INVALID_SNAPSHOT",
        )
    artifacts = dict(snapshot.get("artifacts") or {})
    records = dict(snapshot.get("evidence") or {})
    known_claims = dict(snapshot.get("claims") or {})
    withdrawn = {str(value) for value in snapshot.get("withdrawn") or ()}

    raw_candidates, batch_forged, batch_problems = _split_batch(candidate_batch)
    claims: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for key in batch_forged:
        rejected.append({
            "index": 0,
            "code": RejectionCode.FORGED_IDENTIFIER,
            "detail": (
                f"the batch carries {key!r}, which is not part of a candidate batch. Identity, relations "
                "and the update package are minted here and by the store, never supplied by a model."
            ),
        })
    for problem in batch_problems:
        rejected.append({"index": 0, "code": RejectionCode.INVALID_CANDIDATE, "detail": problem})

    for index, item in enumerate(raw_candidates, start=1):
        raw = item if isinstance(item, Mapping) else None
        try:
            candidate = _coerce_candidate(item)
        except KnowledgeError as error:
            rejected.append({
                "index": index,
                "code": RejectionCode.INVALID_CANDIDATE,
                "detail": str(error),
            })
            continue

        found: list[tuple[str, str]] = []
        forged = _forged_fields(raw)
        if forged:
            found.append((
                RejectionCode.FORGED_IDENTIFIER,
                "the candidate supplies " + ", ".join(forged)
                + "; identifiers and the actor are minted by the pipeline and never accepted from an answer",
            ))
        if screen_material(candidate.statement):
            found.append((
                RejectionCode.INSTRUCTION_FROM_MATERIAL,
                "the statement repeats an instruction found in the material, and material is data",
            ))

        support = _support_names(snapshot, _resolved_ids(candidate, records))
        for name in candidate.state.required_support():
            if name in support:
                continue
            if name == "adoption_record":
                found.append((
                    RejectionCode.FABRICATED_ADOPTION,
                    "the candidate is an adopted decision and no adoption record exists for its evidence",
                ))
            elif name == "verification_record":
                found.append((
                    RejectionCode.FABRICATED_VERIFICATION,
                    "the candidate is verified and no verification record exists for its evidence",
                ))
            else:
                found.append((
                    RejectionCode.UNSUPPORTED_ASSERTION,
                    f"the candidate requires a {name} and the snapshot holds none",
                ))

        if candidate.state.knowledge_kind == "rationale" and candidate.state.derivation == "explicit":
            if not candidate.evidence_refs or candidate.inference_note.strip():
                found.append((
                    RejectionCode.RECONSTRUCTED_REASON_AS_QUOTE,
                    "a reconstructed reason cannot be an explicit quotation, because the material does not "
                    "record it",
                ))

        texts, evidence_problems = _recover_texts(candidate, records, artifacts, scope, withdrawn, snapshot)
        found.extend(evidence_problems)

        for premise in candidate.premise_claim_version_ids:
            if premise not in known_claims:
                found.append((
                    RejectionCode.UNSUPPORTED_ASSERTION,
                    f"premise {premise} is not a recorded claim version this run was given",
                ))

        joined = "\n".join(texts.values())
        if joined:
            missing = _missing_qualifiers(candidate, joined)
            if missing:
                found.append((
                    RejectionCode.MISSING_QUALIFIER,
                    "the material qualifies the statement with " + ", ".join(missing)
                    + " and the candidate does not carry it",
                ))
            denial = _denial(candidate, joined)
            if denial:
                found.append((
                    RejectionCode.CONTRADICTED_BY_EVIDENCE,
                    f"the cited material denies the statement: {denial}",
                ))

        attributes: dict[str, Any] = {}
        if candidate.disposition == "KEEP":
            try:
                attributes = validate_attributes(candidate.state.knowledge_kind, candidate.attributes)
            except KnowledgeError as error:
                found.append((RejectionCode.INVALID_CANDIDATE, str(error)))
            else:
                premise_text = "\n".join(
                    str(known_claims[premise].get("statement", ""))
                    for premise in candidate.premise_claim_version_ids
                    if premise in known_claims
                )
                found.extend(_keep_problems(candidate, texts, premise_text, attributes))

        if found:
            ordered = _ordered(found)
            rejected.append({
                "index": index,
                "batch_id": candidate.batch_id,
                "code": ordered[0][0],
                "reason_codes": [code for code, _detail in ordered],
                "statement": candidate.statement,
                "detail": "; ".join(detail for _code, detail in ordered),
            })
            continue

        if not candidate.disposition:
            raise KnowledgeError(
                f"Candidate {index} has no disposition. Every candidate is KEEP, REVIEW or DROP, and an "
                "undispositioned candidate is an error rather than a silent pass.",
                code="UNDISPOSITIONED_CANDIDATE",
            )

        support_list = sorted(support)
        if candidate.disposition == "DROP":
            dropped.append(_report_item(candidate, index, support_list))
            continue
        if candidate.disposition == "REVIEW":
            reviews.append(_review_item(candidate, index, support_list, scope))
            continue

        claim, claim_relations = _claim_for(
            candidate,
            index=index,
            scope=scope,
            run_id=run_id,
            evidence_texts=texts,
            support=support_list,
            attributes=attributes,
        )
        claims.append(claim)
        relations.extend(claim_relations)

    changeset = build_change_set(
        knowledge_space_id=scope.knowledge_space_id,
        project_id=scope.project_id,
        run_id=run_id,
        claims=claims,
        relations=relations,
        topics=[],
        reviews=reviews,
        dropped=dropped,
        base_version=int(snapshot.get("base_version", 0) or 0),
    )
    return {"changeset": changeset, "rejected": rejected, "reviews": reviews, "dropped": dropped}


def _extract_batches(
    *,
    scope: Scope,
    batches: Sequence[Mapping[str, Any]],
    purpose: str,
    roles: ModelRoles,
    history_reader: Callable[[Scope, ClaimCandidate], Sequence[Mapping[str, Any]]],
    project_context: Sequence[Any],
) -> dict[str, Any]:
    candidates: list[ClaimCandidate] = []
    records: list[dict[str, Any]] = []
    unfinished: list[str] = []

    for batch in batches:
        batch_id = str(batch["batch_id"])
        materials = [dict(material) for material in batch.get("materials") or ()]
        notes: list[dict[str, Any]] = []
        record: dict[str, Any] = {
            "batch_id": batch_id,
            "index": int(batch.get("index") or len(records) + 1),
            "status": "ok",
            "chunk_ids": [material["chunk_id"] for material in materials],
            "evidence_ids": [material["evidence_id"] for material in materials],
            "source_ids": list(batch.get("source_ids") or ()),
            "chars": int(batch.get("chars") or 0),
            "candidate_count": 0,
            "invalid": [],
            "ignored_fields": [],
            "suspicious": screen_materials(materials),
            "notes": notes,
            "error": "",
        }
        try:
            request = wiki_prompts.build_role_request(
                wiki_prompts.ROLE_DISCOVERY,
                materials=materials,
                purpose=purpose,
                project_context=project_context,
            )
            answer = _invoke(roles.callable_for(wiki_prompts.ROLE_DISCOVERY), request, purpose, materials)
            raw_candidates = _answer_candidates(answer, wiki_prompts.ROLE_DISCOVERY)
        except Exception as error:  # a model boundary: any failure is a retryable batch
            record["status"] = "unfinished"
            record["error"] = f"{type(error).__name__}: {error}"
            unfinished.append(batch_id)
            records.append(record)
            continue

        for position, raw in enumerate(raw_candidates, start=1):
            candidate, ignored, problem = _normalise_candidate(raw, batch_id=batch_id)
            if ignored:
                record["ignored_fields"].append({"index": position, "fields": ignored})
            if candidate is None:
                record["invalid"].append({
                    "index": position,
                    "code": RejectionCode.INVALID_CANDIDATE,
                    "detail": problem,
                })
                continue
            candidates.append(_stage_candidate(
                candidate,
                materials=materials,
                scope=scope,
                purpose=purpose,
                roles=roles,
                history_reader=history_reader,
                project_context=project_context,
                notes=notes,
            ))

        record["candidate_count"] = sum(
            1 for candidate in candidates if candidate.batch_id == batch_id
        )
        records.append(record)

    return {"candidates": candidates, "batches": records, "unfinished": unfinished}


def _stage_candidate(
    candidate: ClaimCandidate,
    *,
    materials: Sequence[Mapping[str, Any]],
    scope: Scope,
    purpose: str,
    roles: ModelRoles,
    history_reader: Callable[[Scope, ClaimCandidate], Sequence[Mapping[str, Any]]],
    project_context: Sequence[Any],
    notes: list[dict[str, Any]],
) -> ClaimCandidate:
    """Synthesis, then grounding, then the value gate, in that order.

    Synthesis comes first because it decides which premises the claim actually rests on,
    and the later stages judge the claim against those and no others.
    """

    incomplete = False
    if _needs_synthesis(candidate):
        candidate = _apply_synthesis(
            candidate,
            materials=materials,
            scope=scope,
            purpose=purpose,
            roles=roles,
            history_reader=history_reader,
            project_context=project_context,
            notes=notes,
        )
        incomplete = candidate.state.grounding_status == "unsupported"

    grounding_negative = False
    verdict = _ask_role(
        roles,
        wiki_prompts.ROLE_GROUNDING,
        candidate=candidate,
        materials=materials,
        history=(),
        purpose=purpose,
        project_context=project_context,
        notes=notes,
    )
    if isinstance(verdict, Mapping):
        if verdict.get("contradiction") or verdict.get("supported") is False:
            grounding_negative = True
        qualifiers = [str(item) for item in verdict.get("missing_qualifiers") or () if str(item).strip()]
        unsupported = [str(item) for item in verdict.get("unsupported_claims") or () if str(item).strip()]
        if qualifiers or unsupported:
            grounding_negative = True
        if grounding_negative:
            notes.append({
                "stage": wiki_prompts.ROLE_GROUNDING,
                "statement": candidate.statement,
                "note": str(verdict.get("notes") or ""),
                "missing_qualifiers": qualifiers,
                "unsupported_claims": unsupported,
            })

    disposition, reason_codes = _value_gate(
        candidate,
        _cited_texts(candidate, materials),
        _anchor_text(scope, project_context),
        grounding_negative=grounding_negative or incomplete,
    )
    veto = _ask_role(
        roles,
        wiki_prompts.ROLE_VALUE,
        candidate=candidate,
        materials=materials,
        history=(),
        purpose=purpose,
        project_context=project_context,
        notes=notes,
    )
    disposition, reason_codes = _merge_value(disposition, reason_codes, veto, notes, candidate)

    if not candidate.evidence_refs and not candidate.premise_claim_version_ids:
        grounding_status = "unsupported"
    elif grounding_negative:
        grounding_status = "needs_revalidation"
    else:
        grounding_status = "grounded"
    return replace(
        candidate,
        state=replace(candidate.state, grounding_status=grounding_status),
        disposition=disposition,
        reason_codes=tuple(reason_codes),
    )


def _needs_synthesis(candidate: ClaimCandidate) -> bool:
    if candidate.state.derivation == "synthesized":
        return True
    if candidate.premise_claim_version_ids:
        return True
    return len(set(candidate.evidence_refs)) > 1


def _apply_synthesis(
    candidate: ClaimCandidate,
    *,
    materials: Sequence[Mapping[str, Any]],
    scope: Scope,
    purpose: str,
    roles: ModelRoles,
    history_reader: Callable[[Scope, ClaimCandidate], Sequence[Mapping[str, Any]]],
    project_context: Sequence[Any],
    notes: list[dict[str, Any]],
) -> ClaimCandidate:
    """Let the synthesis role name every premise, then check it named only real ones.

    A premise the run cannot resolve is not quietly kept as prose. It is dropped and the
    candidate is marked ungrounded, so a conclusion missing half of its support is
    reviewed rather than published looking complete.
    """

    history = _history_for(history_reader, scope, candidate, notes)
    answer = _ask_role(
        roles,
        wiki_prompts.ROLE_SYNTHESIS,
        candidate=candidate,
        materials=materials,
        history=history,
        purpose=purpose,
        project_context=project_context,
        notes=notes,
    )
    if not isinstance(answer, Mapping):
        notes.append({
            "stage": wiki_prompts.ROLE_SYNTHESIS,
            "statement": candidate.statement,
            "note": "no synthesis answer, so the premises are unknown",
        })
        return replace(candidate, state=replace(candidate.state, grounding_status="unsupported"))

    known_evidence = {str(material["evidence_id"]) for material in materials}
    known_versions = {str(claim.get("claim_version_id", "")) for claim in history}
    premises = [str(item) for item in answer.get("premises") or () if str(item).strip()]
    versions = [str(item) for item in answer.get("premise_claim_version_ids") or () if str(item).strip()]
    dropped = [item for item in premises if item not in known_evidence]
    dropped.extend(item for item in versions if item not in known_versions)

    evidence_refs = tuple(premises) if "premises" in answer else tuple(candidate.evidence_refs)
    premise_ids = (
        tuple(item for item in versions if item in known_versions)
        if "premise_claim_version_ids" in answer
        else tuple(candidate.premise_claim_version_ids)
    )
    inference_note = str(answer.get("inference_note") or candidate.inference_note or "").strip()
    assumptions = tuple(str(item) for item in answer.get("assumptions") or candidate.assumptions or ())

    updated = replace(
        candidate,
        evidence_refs=evidence_refs,
        premise_claim_version_ids=premise_ids,
        inference_note=inference_note,
        assumptions=assumptions,
    )
    complete = bool(updated.evidence_refs or updated.premise_claim_version_ids)
    if dropped or not complete or not inference_note:
        if dropped:
            notes.append({
                "stage": wiki_prompts.ROLE_SYNTHESIS,
                "statement": candidate.statement,
                "note": (
                    "the synthesis named premises this run cannot resolve, so the conclusion is not "
                    "reviewable and no premise is invented to replace them"
                ),
            })
        return replace(updated, state=replace(updated.state, grounding_status="unsupported"))
    return updated


def _history_for(
    history_reader: Callable[[Scope, ClaimCandidate], Sequence[Mapping[str, Any]]],
    scope: Scope,
    candidate: ClaimCandidate,
    notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ask the caller for the recorded claims this candidate may derive from.

    The pipeline never reads a store. The caller supplies the reader, so the same
    extraction runs against a database, a fixture, or nothing at all.
    """

    if history_reader is None:
        return []
    try:
        history = list(history_reader(scope, candidate) or ())
    except Exception as error:  # a boundary the caller owns; a miss is not an invention
        notes.append({
            "stage": "history",
            "statement": candidate.statement,
            "note": f"history lookup failed: {type(error).__name__}: {error}",
        })
        return []
    return [dict(claim) for claim in history]


PROVENANCE_KEY = "stage_provenance"
"""The key a stage-provenance entry carries, so it is separable from a problem note."""


def _ask_role(
    roles: ModelRoles,
    role: str,
    *,
    candidate: ClaimCandidate,
    materials: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    purpose: str,
    project_context: Sequence[Any],
    notes: list[dict[str, Any]],
) -> Any:
    model = roles.callable_for(role)
    if model is None:
        notes.append({"stage": role, "statement": candidate.statement, "note": "no model role configured"})
        return None
    try:
        request = wiki_prompts.build_role_request(
            role,
            materials=materials,
            candidate=candidate,
            history=history,
            purpose=purpose,
            project_context=project_context,
        )
        answer = _invoke(model, request, purpose, materials)
        record = answer.get("_stage") if isinstance(answer, Mapping) else None
        if isinstance(record, Mapping):
            # Which model answered, under which prompt version, at what reported
            # cost. The caller turns this into the run's cost record.
            notes.append({PROVENANCE_KEY: dict(record)})
        return answer
    except Exception as error:
        notes.append({
            "stage": role,
            "statement": candidate.statement,
            "note": f"{type(error).__name__}: {error}",
        })
        return None


def _merge_value(
    disposition: str,
    reason_codes: list[str],
    veto: Any,
    notes: list[dict[str, Any]],
    candidate: ClaimCandidate,
) -> tuple[str, list[str]]:
    """A model may lower a disposition and may never raise one.

    Value is a policy question. The pipeline can always say the material does not carry a
    claim, but a model saying "keep this" is not evidence that the project should, so its
    answer is only ever read as a veto.
    """

    if not isinstance(veto, Mapping):
        return disposition, reason_codes
    proposed = str(veto.get("disposition") or "").strip().upper()
    code = str(veto.get("reason_code") or "").strip()
    if proposed not in VALUE_GATE_DECISIONS:
        notes.append({
            "stage": wiki_prompts.ROLE_VALUE,
            "statement": candidate.statement,
            "note": f"ignored an illegal disposition: {proposed!r}",
        })
        return disposition, reason_codes
    if code and code not in VALUE_GATE_REASON_CODES:
        notes.append({
            "stage": wiki_prompts.ROLE_VALUE,
            "statement": candidate.statement,
            "note": f"ignored an unknown reason code: {code!r}",
        })
        code = ""
    if _value_strength(proposed) < _value_strength(disposition):
        return proposed, [item for item in [code, *reason_codes] if item]
    return disposition, reason_codes


def _value_strength(disposition: str) -> int:
    return {"DROP": 0, "REVIEW": 1, "KEEP": 2}.get(disposition, -1)


EPHEMERAL_PATTERNS = (
    re.compile(
        r"\b(?:today|yesterday|tonight|this morning|this afternoon|just now|earlier today|"
        r"right now|for now|temporarily|temporary)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:we|i|the team)\s+(?:just\s+)?(?:tested|tried|ran|benchmarked|experimented|trialled|piloted)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bTIL\b|\bFWIW\b"),
    re.compile(r"(?:今天|昨天|刚刚|刚才|本次|临时|试了|试了一下|跑了一次|测了一下)"),
)

GENERIC_PATTERNS = (
    re.compile(
        r"\bin general\b|\bgenerally\b|\btypically\b|\busually\b|\bfor example\b|\bby default\b|"
        r"\bmany teams\b|\bsome teams\b|\bmost tools\b|\bis a kind of\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:一般来说|通常来说|一般情况下|众所周知|泛指|背景知识|一般而言)"),
)

DURABLE_KINDS = ("decision", "constraint", "process", "rationale", "architecture", "distinction")


def _value_gate(
    candidate: ClaimCandidate,
    texts: Sequence[str],
    anchor: str,
    *,
    grounding_negative: bool = False,
) -> tuple[str, list[str]]:
    """Decide what the project should keep, from the claim and the text it cites.

    A total function: every candidate leaves with a disposition, because a caller has to
    be able to say what happened to each one. The rules are stated here rather than asked
    of a model, so a disposition is reproducible and a reviewer can argue with the rule
    instead of with a sample.
    """

    kind = candidate.state.knowledge_kind
    statement = candidate.statement
    material = "\n".join(texts)

    if not candidate.evidence_refs and not candidate.premise_claim_version_ids:
        disposition, reason_codes = "REVIEW", ["insufficient_context"]
    elif kind not in DURABLE_KINDS and (
        _matches(EPHEMERAL_PATTERNS, statement) or _matches(EPHEMERAL_PATTERNS, material)
    ):
        disposition, reason_codes = "DROP", ["ephemeral_activity"]
    elif kind == "constraint":
        disposition, reason_codes = "KEEP", ["reusable_constraint"]
    elif kind == "decision":
        if candidate.state.decision_state == "proposed" and candidate.attribution.asserted_by in ("", "unknown"):
            disposition, reason_codes = "REVIEW", ["ambiguous_adoption"]
        else:
            disposition, reason_codes = "KEEP", ["reusable_decision"]
    elif kind == "rationale":
        disposition, reason_codes = "KEEP", ["explains_choice"]
    elif kind == "process":
        disposition, reason_codes = "KEEP", ["reproducible_process"]
    elif kind == "distinction":
        disposition, reason_codes = "KEEP", ["important_distinction"]
    elif kind == "open_question":
        disposition, reason_codes = "KEEP", ["active_question"]
    elif kind == "architecture":
        disposition, reason_codes = "KEEP", ["supporting_context"]
    elif _matches(GENERIC_PATTERNS, statement) and not _anchored(statement, anchor):
        disposition, reason_codes = "DROP", ["generic_background"]
    else:
        disposition, reason_codes = "KEEP", ["supporting_context"]

    if grounding_negative and disposition == "KEEP":
        return "REVIEW", [*reason_codes, "insufficient_context"]
    return disposition, reason_codes


def _matches(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text or "") for pattern in patterns)


def _anchored(statement: str, anchor: str) -> bool:
    if not anchor.strip():
        return False
    return any(term in anchor for term in _key_terms(statement))


def _anchor_text(scope: Scope, project_context: Sequence[Any]) -> str:
    return " ".join([scope.project_id, *(str(item) for item in project_context)])


def _recover_texts(
    candidate: ClaimCandidate,
    records: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    scope: Scope,
    withdrawn: set[str],
    snapshot: Mapping[str, Any],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Recover the exact text behind every citation, or name why it cannot be recovered.

    The spans come back out of the frozen artifact, so what the check reads is what the
    source says. A citation that does not resolve is a refusal, not an empty string. The
    texts are keyed by the record's own evidence id, so a change set cites the address the
    store registered even when the pipeline worked under an alias.
    """

    texts: dict[str, str] = {}
    problems: list[tuple[str, str]] = []
    for reference in candidate.evidence_refs:
        record = records.get(reference)
        if record is None:
            problems.append((
                RejectionCode.UNSUPPORTED_ASSERTION,
                f"evidence {reference} is not part of this run",
            ))
            continue
        try:
            record = _coerce_record(record)
            artifact = artifacts.get(record.artifact_id)
            recovered = evidence.recover(
                record=record,
                artifact=_coerce_artifact(artifact),
                expected_project_id=scope.project_id,
                source_withdrawn=(record.artifact_id in withdrawn or record.evidence_id in withdrawn),
                raw_available=bool(snapshot.get("raw_available", True)),
            )
        except KnowledgeError as error:
            problems.append((
                RejectionCode.UNSUPPORTED_ASSERTION,
                f"evidence {reference}: {error}",
            ))
            continue
        except evidence.EvidenceError as error:
            code = (
                RejectionCode.CROSS_PROJECT_REFERENCE
                if error.code == "SCOPE_MISMATCH"
                else RejectionCode.UNSUPPORTED_ASSERTION
            )
            problems.append((code, f"evidence {record.evidence_id} cannot be recovered: {error.code}"))
            continue
        texts[str(record.evidence_id)] = recovered.exact_text
    return texts, problems


def _ordered(found: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Rejections in the order `REJECTION_ORDER` states, so two runs report the same cause."""

    severity = {code: position for position, code in enumerate(REJECTION_ORDER)}
    # Every refusal the spec names has a position in that table, and this is where the two
    # lists are held together instead of drifting apart on the next edit.
    missing = [code for code in ZERO_TOLERANCE_CODES if code not in severity]
    if missing:
        raise KnowledgeError(
            f"REJECTION_ORDER carries no severity for {', '.join(missing)}.",
            code="INVALID_REJECTION_ORDER",
        )
    return sorted(
        found,
        key=lambda item: (severity.get(item[0], len(REJECTION_ORDER)), item[1]),
    )


def _keep_problems(
    candidate: ClaimCandidate,
    texts: Mapping[str, str],
    premise_text: str,
    attributes: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """The checks that only apply to a candidate the value gate would publish."""

    problems: list[tuple[str, str]] = []
    covered = "\n".join([*texts.values(), premise_text])
    exempt = candidate.state.knowledge_kind == "rationale" and candidate.state.derivation == "synthesized"
    if not exempt:
        wording = "\n".join([candidate.statement, *candidate.conditions])
        if not _covered(wording, covered, minimum=MIN_STATEMENT_COVERAGE):
            problems.append((
                RejectionCode.UNSUPPORTED_ASSERTION,
                "the cited material does not carry enough of the statement for the wording to be a record of it",
            ))
    if candidate.state.derivation == "synthesized" and not candidate.inference_note.strip():
        problems.append((
            RejectionCode.UNSUPPORTED_ASSERTION,
            "a synthesized claim needs an inference note a reviewer can check",
        ))
    if candidate.state.knowledge_kind == "process":
        problems.extend(_process_problems(attributes, covered))
    return problems


def _process_problems(
    attributes: Mapping[str, Any],
    material: str,
) -> list[tuple[str, str]]:
    """A process claim has to be reproducible from the material, not summarised from it.

    Splitting a described process into steps that lost their order and their dependencies
    is the failure this catches: the claim then asserts a procedure the material does not
    describe.
    """

    steps = list(attributes.get("steps") or ())
    if not steps:
        return [(
            RejectionCode.UNSUPPORTED_ASSERTION,
            "a process claim must carry its steps; the material describes an order the claim does not keep",
        )]
    problems: list[tuple[str, str]] = []
    described = _ordered_steps(material)
    if described and len(steps) < len(described):
        problems.append((
            RejectionCode.UNSUPPORTED_ASSERTION,
            f"the material records {len(described)} ordered steps and the claim carries {len(steps)}; an "
            "incomplete process is not the process the material describes",
        ))
    for step in steps:
        local = " ".join([str(step["action"]), *(str(item) for item in step["preconditions"])])
        if ORDER_MARKER_RE.search(local) and not step["depends_on"]:
            problems.append((
                RejectionCode.UNSUPPORTED_ASSERTION,
                f"step {step['order']} states a dependency the claim records no depends_on for",
            ))
        if not _covered(str(step["action"]), material, minimum=MIN_STATEMENT_COVERAGE):
            problems.append((
                RejectionCode.UNSUPPORTED_ASSERTION,
                f"step {step['order']} is not described by the cited material",
            ))
    return problems


def _claim_for(
    candidate: ClaimCandidate,
    *,
    index: int,
    scope: Scope,
    run_id: str,
    evidence_texts: Mapping[str, str],
    support: Sequence[str],
    attributes: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Mint the identity of one publishable claim, and the edges it derives from.

    The ids are derived from the scope, the run, the batch and the wording, so the same
    material re-extracted produces the same ids and a second run is an update rather than
    a duplicate. Nothing a model returned is copied into them.
    """

    fingerprint = (
        scope.knowledge_space_id,
        scope.project_id,
        run_id,
        candidate.batch_id,
        index,
        candidate.statement,
    )
    claim_version_id = _mint("clv_", *fingerprint)
    basis = "recorded" if candidate.state.derivation == "explicit" else "reconstructed"
    origin = {
        "origin_id": _mint("org_", *fingerprint, candidate.state.derivation),
        "claim_version_id": claim_version_id,
        "derivation": candidate.state.derivation,
        "evidence_refs": sorted(evidence_texts),
        "premise_claim_version_ids": list(candidate.premise_claim_version_ids),
        "inference_note": candidate.inference_note,
        "assumptions": list(candidate.assumptions),
        "support_group_id": _mint("sgr_", *fingerprint),
        "origin_status": "active",
        "attribution": candidate.attribution.as_dict(),
    }
    claim = {
        "claim_id": None,
        "claim_version_id": claim_version_id,
        "statement": candidate.statement,
        "state": candidate.state.as_dict(),
        "conditions": list(candidate.conditions),
        "subjects": list(candidate.subjects),
        "attribution": candidate.attribution.as_dict(),
        "attributes": dict(attributes),
        "origins": [origin],
        "support": list(support),
        "evidence_refs": sorted(evidence_texts),
        "disposition": candidate.disposition,
        "reason_codes": list(candidate.reason_codes),
        "batch_id": candidate.batch_id,
        "explanation_basis": basis,
    }
    relations = [
        {
            "relation_id": _mint("rel_", *fingerprint, premise),
            "relation_type": "derived_from",
            "from_claim_version_id": claim_version_id,
            "to_claim_version_id": premise,
            "relation_status": "proposed",
            "origin_evidence_refs": sorted(evidence_texts),
            "recorded_by": "system",
        }
        for premise in candidate.premise_claim_version_ids
    ]
    return claim, relations


def _report_item(candidate: ClaimCandidate, index: int, support: Sequence[str]) -> dict[str, Any]:
    return {
        "index": index,
        "batch_id": candidate.batch_id,
        "unit_id": candidate.statement[:120],
        "statement": candidate.statement,
        "disposition": candidate.disposition,
        "reason_codes": list(candidate.reason_codes),
        "support": list(support),
        "evidence_refs": list(candidate.evidence_refs),
        "grounding_status": candidate.state.grounding_status,
    }


def _review_item(candidate: ClaimCandidate, index: int, support: Sequence[str], scope: Scope) -> dict[str, Any]:
    seed = (scope.knowledge_space_id, scope.project_id, index, candidate.statement)
    return {
        "review_id": _mint("revq_", *seed),
        "subject_kind": "claim",
        "subject_id": _mint("clm_", *seed),
        "subject_version": _mint("clv_", *seed),
        "question": _review_question(candidate),
        "trigger_code": _review_trigger(candidate),
        "action": "retain",
        "index": index,
        "batch_id": candidate.batch_id,
        "statement": candidate.statement,
        "disposition": candidate.disposition,
        "reason_codes": list(candidate.reason_codes),
        "candidates": [],
        "evidence_refs": list(candidate.evidence_refs),
        "premise_claim_version_ids": list(candidate.premise_claim_version_ids),
        "support": list(support),
        "grounding_status": candidate.state.grounding_status,
        "impact": {
            "knowledge_kind": candidate.state.knowledge_kind,
            "decision_state": candidate.state.decision_state,
        },
    }


REVIEW_TRIGGERS = ("insufficient_context", "ambiguous_adoption", "ambiguous_identity")


def _review_trigger(candidate: ClaimCandidate) -> str:
    """The reason a reviewer opens this item, which is not the value gate's keep reason."""

    codes = list(candidate.reason_codes)
    for code in REVIEW_TRIGGERS:
        if code in codes:
            return code
    return codes[0] if codes else "insufficient_context"


def _review_question(candidate: ClaimCandidate) -> str:
    """One question a reviewer can answer, phrased from why the candidate is under review."""

    codes = set(candidate.reason_codes)
    if "insufficient_context" in codes:
        return (
            "This proposition is not carried by the material this run read. Cite a source that carries it, "
            "or leave it unpublished?"
        )
    if "ambiguous_adoption" in codes:
        return "Who proposed this, and has anyone adopted it?"
    if "ambiguous_identity" in codes:
        return "Which existing subject does this refer to?"
    return "Should this proposition be kept?"


def _resolve_config(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(config or {})
    target_chars = int(raw.get("target_chars", DEFAULT_TARGET_CHARS))
    max_chars = int(raw.get("max_chars", DEFAULT_MAX_CHARS))
    budget = int(raw.get("batch_budget_chars", DEFAULT_BATCH_BUDGET_CHARS))
    base_version = int(raw.get("base_version", 0))
    if target_chars < 1 or max_chars < 1 or budget < 1:
        raise KnowledgeError("Chunking and batch budgets must be at least one.", code="INVALID_CONFIG")
    if max_chars < target_chars:
        raise KnowledgeError("max_chars cannot be smaller than target_chars.", code="INVALID_CONFIG")
    if budget < max_chars:
        raise KnowledgeError(
            "The batch budget cannot be smaller than the chunk ceiling; that asks for a batch no chunk fits "
            "in, which is how a chunk ends up truncated.",
            code="INVALID_CONFIG",
        )
    extra = {
        str(key): str(value)
        for key, value in sorted(raw.items())
        if key not in {"target_chars", "max_chars", "batch_budget_chars", "base_version"}
    }
    fingerprint = evidence.sha256_text(json.dumps(
        {
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "target_chars": target_chars,
            "max_chars": max_chars,
            "batch_budget_chars": budget,
            "extra": extra,
        },
        sort_keys=True,
        ensure_ascii=False,
    ))
    return {
        "target_chars": target_chars,
        "max_chars": max_chars,
        "batch_budget_chars": budget,
        "base_version": base_version,
        "extra": extra,
        "config_fingerprint": fingerprint,
    }


def _normalise_source_input(raw: Any, position: int) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise KnowledgeError(f"Source input {position} must be an object.", code="INVALID_MATERIAL")
    source_id = str(raw.get("source_id") or "").strip()
    if not source_id:
        raise KnowledgeError(f"Source input {position} has no source_id.", code="INVALID_MATERIAL")
    content = str(raw.get("content") or "")
    if not content.strip():
        raise KnowledgeError(f"Source {source_id} carries no content.", code="INVALID_MATERIAL")
    return {
        "source_id": source_id,
        "kind": str(raw.get("kind") or "material").strip() or "material",
        "label": str(raw.get("label") or source_id).strip() or source_id,
        "content": content,
    }


def _freeze_chunk(
    *,
    scope: Scope,
    artifact: Any,
    chunk: Any,
    label: str,
    evidence_id: str | None,
) -> Any:
    """Build the citation for one rendered chunk, hashing what it really covers.

    `chunk.evidence()` is what the source contains. A renderer's re-emitted fence or
    header is not part of it, so a reconstructed scaffold can never be cited as source
    text, and a caller that already registered the id gets a record that agrees with it.

    The citation keeps whichever address it was given, because the id the store
    registered is the id the claim must cite.
    """

    spans = tuple(chunk.evidence())
    hashes = tuple(
        evidence.span_hash(artifact.normalized_text, start, end) for start, end in spans
    )
    if evidence_id:
        return evidence.EvidenceRecord(
            evidence_id=evidence_id,
            project_id=scope.project_id,
            artifact_id=artifact.artifact_id,
            spans=spans,
            span_hashes=hashes,
            heading_path=tuple(chunk.heading_path),
            structural_context_refs=_structural_refs(artifact, chunk),
            label=label,
        )
    minted = evidence.make_evidence(
        project_id=scope.project_id,
        artifact=artifact,
        spans=spans,
        heading_path=tuple(chunk.heading_path),
        structural_context_refs=_structural_refs(artifact, chunk),
        label=label,
    )
    return minted


def _structural_refs(artifact: Any, chunk: Any) -> tuple[int, ...]:
    """The frozen block a reconstruction needs to be readable later.

    A split table piece was rendered with its header repeated, and a reader recovering the
    citation needs that header. The block index is where the frozen snapshot keeps it.
    """

    if not getattr(chunk, "context_spans", ()):
        return ()
    blocks = list((artifact.structure or {}).get("blocks", []))
    start = int(getattr(chunk, "start", 0))
    end = int(getattr(chunk, "end", start + 1))
    for index, block in enumerate(blocks):
        block_start = int(block.get("start", 0))
        block_end = int(block.get("end", 0))
        if block_start <= start < block_end or block_start <= end - 1 < block_end:
            return (index,)
    return ()


def _material(artifact: Any, chunk: Any, evidence_id: str, source_id: str) -> dict[str, Any]:
    return {
        "chunk_id": str(chunk.chunk_id),
        "source_id": source_id,
        "artifact_id": artifact.artifact_id,
        "evidence_id": evidence_id,
        "text": str(chunk.text),
        "heading_path": list(chunk.heading_path),
        "verbatim": bool(chunk.verbatim),
        "render_recipe": str(chunk.render_recipe),
        "index": int(chunk.index),
        "start": int(chunk.start),
        "end": int(chunk.end),
    }


def _invoke(
    model: ModelFn | None,
    request: dict[str, Any],
    purpose: str,
    materials: Sequence[Mapping[str, Any]],
) -> Any:
    if model is None:
        raise KnowledgeError("No model role is configured for this request.", code="MISSING_MODEL_ROLE")
    return model(request, purpose, [dict(material) for material in materials])


def _answer_candidates(answer: Any, role: str) -> list[Any]:
    if not isinstance(answer, Mapping):
        raise KnowledgeError(
            f"The {role} answer must be a JSON object; got {type(answer).__name__}.",
            code="INVALID_ANSWER",
        )
    raw = answer.get("candidates")
    if raw is None:
        raise KnowledgeError(f"The {role} answer carries no candidates list.", code="INVALID_ANSWER")
    if not isinstance(raw, (list, tuple)):
        raise KnowledgeError(f"The {role} answer's candidates must be a list.", code="INVALID_ANSWER")
    return list(raw)


CANDIDATE_FIELDS = (
    "statement",
    "subjects",
    "conditions",
    "attribution",
    "evidence_refs",
    "premise_claim_version_ids",
    "inference_note",
    "assumptions",
    "attributes",
    "disposition",
    "reason_codes",
    "batch_id",
)

STATE_FIELDS = (
    "knowledge_kind",
    "derivation",
    "epistemic_status",
    "lifecycle_status",
    "grounding_status",
    "decision_state",
    "question_state",
)

FORGED_KEYS = frozenset({
    "claim_id",
    "claim_version_id",
    "origin_id",
    "relation_id",
    "artifact_id",
    "source_id",
    "review_id",
    "topic_id",
    "support_group_id",
    "chunk_id",
    "evidence_id",
    "actor",
    "actor_subject",
    "recorded_by",
    "committed_by",
    "update_package",
    "pages",
    "claims",
    "topics",
    "relations",
    "schema_version",
    "base_version",
    "versions",
})

IDENTIFIER_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bclm_[0-9a-f]{32}\b",
        r"\bclv_[0-9a-f]{32}\b",
        r"\borg_[0-9a-f]{32}\b",
        r"\bevd_[0-9a-f]{32}\b",
        r"\bart_[0-9a-f]{32}\b",
        r"\brev_[0-9a-f]{32}\b",
        r"\brel_[0-9a-f]{32}\b",
        r"\brevq_[0-9a-f]{32}\b",
        r"\bsgr_[0-9a-f]{32}\b",
        r"\btop_[0-9a-f]{32}\b",
        r"\bpag_[0-9a-f]{32}\b",
    )
)


def _forged_fields(raw: Mapping[str, Any] | None) -> list[str]:
    """The keys a candidate may not carry, in the order a reader should see them.

    A model that returns an object id, an actor, or a whole update package is trying to
    write something only the pipeline and the store may write. Those fields are refused
    rather than stripped, because stripping hides that the model tried.
    """

    if not isinstance(raw, Mapping):
        return []
    forged: list[str] = []
    for key, value in raw.items():
        name = str(key)
        if name in CANDIDATE_FIELDS or name in STATE_FIELDS or name == "state":
            continue
        if name in FORGED_KEYS:
            forged.append(name)
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        if any(pattern.search(text) for pattern in IDENTIFIER_PATTERNS):
            forged.append(name)
    return sorted(forged)


def _normalise_candidate(raw: Any, *, batch_id: str) -> tuple[ClaimCandidate | None, list[str], str]:
    """One answer entry as a `ClaimCandidate`, plus whatever it tried to carry besides.

    Keys outside the candidate contract are reported, not copied. A candidate that does
    not satisfy the contract at all is reported as invalid rather than repaired.
    """

    if not isinstance(raw, Mapping):
        return None, [], f"candidate is {type(raw).__name__}, not an object"
    ignored = sorted({
        str(key)
        for key in raw
        if str(key) not in CANDIDATE_FIELDS and str(key) not in STATE_FIELDS and str(key) != "state"
    })
    try:
        candidate = _build_candidate(raw, batch_id=batch_id)
    except KnowledgeError as error:
        return None, ignored, str(error)
    except (TypeError, ValueError) as error:
        return None, ignored, f"{type(error).__name__}: {error}"
    return candidate, ignored, ""


def _build_candidate(raw: Mapping[str, Any], *, batch_id: str) -> ClaimCandidate:
    state = raw.get("state")
    if isinstance(state, Mapping):
        axes = {name: state.get(name) for name in STATE_FIELDS if name in state}
    else:
        axes = {name: raw.get(name) for name in STATE_FIELDS if name in raw}
    for required in ("knowledge_kind", "derivation", "epistemic_status"):
        if not axes.get(required):
            raise KnowledgeError(f"candidate is missing {required}", code="INVALID_CANDIDATE")
    axes["lifecycle_status"] = axes.get("lifecycle_status") or "active"
    axes["grounding_status"] = axes.get("grounding_status") or "grounded"
    axes["decision_state"] = axes.get("decision_state") or None
    axes["question_state"] = axes.get("question_state") or None
    return ClaimCandidate(
        statement=str(raw.get("statement") or ""),
        state=ClaimState(**axes),
        subjects=tuple(str(item) for item in raw.get("subjects") or ()),
        conditions=tuple(str(item) for item in raw.get("conditions") or ()),
        attribution=_attribution(raw.get("attribution")),
        evidence_refs=tuple(str(item) for item in raw.get("evidence_refs") or ()),
        premise_claim_version_ids=tuple(
            str(item) for item in raw.get("premise_claim_version_ids") or ()
        ),
        inference_note=str(raw.get("inference_note") or ""),
        assumptions=tuple(str(item) for item in raw.get("assumptions") or ()),
        attributes=dict(raw.get("attributes") or {}),
        disposition=str(raw.get("disposition") or "").upper(),
        reason_codes=tuple(str(item) for item in raw.get("reason_codes") or ()),
        batch_id=str(raw.get("batch_id") or batch_id),
    )


def _coerce_candidate(item: Any) -> ClaimCandidate:
    if isinstance(item, ClaimCandidate):
        return item
    if isinstance(item, Mapping):
        return _build_candidate(item, batch_id=str(item.get("batch_id") or ""))
    raise KnowledgeError(
        f"A candidate must be a ClaimCandidate or an object; got {type(item).__name__}.",
        code="INVALID_CANDIDATE",
    )


def _attribution(value: Any) -> Any:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise KnowledgeError("attribution must be an object.", code="INVALID_CANDIDATE")
    return Attribution.of(
        value.get("asserted_by", "unknown"),
        value.get("asserted_at"),
        value.get("asserted_at_precision", "unknown"),
    )


def _split_batch(candidate_batch: Any) -> tuple[list[Any], list[str], list[str]]:
    """Normalise a batch of candidates, and name whatever a model wrapped them in."""

    if isinstance(candidate_batch, Mapping):
        if "candidates" in candidate_batch:
            raw = candidate_batch.get("candidates")
            if not isinstance(raw, (list, tuple)):
                return [], [], ["the batch's candidates field is not a list"]
            tolerated = {"candidates", "note", "batch_id", "run_id", "source_ids", "purpose"}
            forged = sorted(str(key) for key in candidate_batch if str(key) not in tolerated)
            return list(raw), forged, []
        if "statement" in candidate_batch:
            return [candidate_batch], [], []
        return [], sorted(str(key) for key in candidate_batch), []
    if isinstance(candidate_batch, (list, tuple)):
        return list(candidate_batch), [], []
    if candidate_batch is None:
        return [], [], []
    return [], [], [f"a candidate batch must be a list or an object; got {type(candidate_batch).__name__}"]


def _cited_texts(candidate: ClaimCandidate, materials: Sequence[Mapping[str, Any]]) -> list[str]:
    refs = {str(reference) for reference in candidate.evidence_refs}
    return [
        str(material.get("text", ""))
        for material in materials
        if str(material.get("evidence_id", "")) in refs
    ]


def _resolved_ids(candidate: ClaimCandidate, records: Mapping[str, Any]) -> list[str]:
    """Every name this candidate's citations are known by, the pipeline's and the store's.

    A support record is keyed by the evidence the store registered, so a check that only
    looked at the alias would miss it.
    """

    ids: set[str] = set()
    for reference in candidate.evidence_refs:
        ids.add(str(reference))
        record = records.get(reference)
        if record is None:
            continue
        if isinstance(record, Mapping):
            ids.add(str(record.get("evidence_id") or ""))
        else:
            ids.add(str(getattr(record, "evidence_id", "") or ""))
    ids.discard("")
    return sorted(ids)


def _support_names(snapshot: Mapping[str, Any], evidence_refs: Sequence[str]) -> set[str]:
    """Which proof records exist for this candidate's evidence, and only those.

    A record keyed by the evidence it proves is the only thing that makes an adoption or a
    verification real, which is why a key of "*" exists for a run-level record and nothing
    else counts.
    """

    available: set[str] = set()
    refs = {str(reference) for reference in evidence_refs}
    for name, key in (
        ("adoption_record", "adoption_records"),
        ("verification_record", "verification_records"),
        ("dispute_record", "dispute_records"),
        ("resolution_record", "resolution_records"),
    ):
        container = snapshot.get(key) or {}
        if isinstance(container, Mapping):
            if container.get("*"):
                available.add(name)
            elif refs and refs & {str(entry) for entry, value in container.items() if value}:
                available.add(name)
        elif container:
            if container is True or refs & {str(value) for value in container}:
                available.add(name)
    return available


def _mint(prefix: str, *parts: Any) -> str:
    return prefix + _digest(*parts)


def _digest(*parts: Any) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(json.dumps(part, default=str, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:32]


def _coerce_scope(value: Any) -> Scope:
    if isinstance(value, Scope):
        return value
    if isinstance(value, Mapping):
        return Scope.of(str(value.get("knowledge_space_id") or ""), str(value.get("project_id") or ""))
    raise KnowledgeError(f"A scope is required; got {type(value).__name__}.", code="INVALID_SCOPE")


def _coerce_artifact(value: Any) -> Any:
    if value is None:
        raise KnowledgeError("An artifact is required to recover evidence text.", code="INVALID_ARTIFACT")
    if isinstance(value, Mapping):
        try:
            return evidence.Artifact(**{key: value[key] for key in ARTIFACT_FIELDS if key in value})
        except TypeError as error:
            raise KnowledgeError(f"Malformed artifact: {error}", code="INVALID_ARTIFACT") from error
    return value


def _coerce_record(value: Any) -> Any:
    if isinstance(value, Mapping):
        try:
            return evidence.EvidenceRecord(
                evidence_id=str(value["evidence_id"]),
                project_id=str(value["project_id"]),
                artifact_id=str(value["artifact_id"]),
                spans=tuple((int(start), int(end)) for start, end in value["spans"]),
                span_hashes=tuple(str(item) for item in value["span_hashes"]),
                heading_path=tuple(str(item) for item in value.get("heading_path") or ()),
                structural_context_refs=tuple(
                    int(item) for item in value.get("structural_context_refs") or ()
                ),
                label=str(value.get("label") or ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise KnowledgeError(f"Malformed evidence record: {error}", code="INVALID_EVIDENCE") from error
    return value


def _require_run(prepared_run: Any) -> Mapping[str, Any]:
    if not isinstance(prepared_run, Mapping):
        raise KnowledgeError(
            f"A prepared run is required; got {type(prepared_run).__name__}.",
            code="INVALID_RUN",
        )
    for key in ("run_id", "scope", "batches"):
        if key not in prepared_run:
            raise KnowledgeError(f"The prepared run carries no {key}.", code="INVALID_RUN")
    return prepared_run


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
_CJK_RE = re.compile(r"[\u3400-\u9fff]{2,}")

_STOPWORDS = frozenset(
    """
    about above after again against almost alone along already also although always among another any
    anyone anything because before being below beside between both cannot could does doing done down
    during each either else enough even ever every everyone everything except first from further have
    having here however into itself just last least less like made make many might more most much must
    near need neither never only other others our ours over own same several shall should since some
    someone something still such than that their theirs them themselves then there these they thing
    think this those though through thus under until upon used using very want were what when whenever
    where whereas which while whom whose will with within without would your yours
    """.split()
)

QUALIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("仅限当前项目", re.compile(r"仅限当前项目")),
    ("暂不", re.compile(r"暂不")),
    ("可能", re.compile(r"可能")),
    ("除非", re.compile(r"除非")),
    ("limited to this project", re.compile(r"limited to this project", re.IGNORECASE)),
    ("only within this project", re.compile(r"only within this project", re.IGNORECASE)),
    ("deferred", re.compile(r"\bdeferred\b", re.IGNORECASE)),
    ("may", re.compile(r"\bmay\b", re.IGNORECASE)),
    ("might", re.compile(r"\bmight\b", re.IGNORECASE)),
    ("unless", re.compile(r"\bunless\b", re.IGNORECASE)),
    ("not yet", re.compile(r"\bnot yet\b", re.IGNORECASE)),
)
"""Wording whose loss changes what a claim means, checked against both languages.

A material that qualifies its statement and a candidate that does not is how a scoped
constraint becomes a universal rule, so the check is on the token, not on the sense.
"""

NEGATION_RE = re.compile(
    r"(?:\b(?:not|never|no|none|cannot|can't|don't|doesn't|didn't|isn't|aren't|won't|"
    r"without|denies?|denied|refuses?|refused)\b|[不未无没非拒])",
    re.IGNORECASE,
)

ORDER_MARKER_RE = re.compile(
    r"(?:\bstep\s*\d|\bafter\b|\bbefore\b|\bonce\b|\bthen\b|\bfollowing\b|\bfirst\b|"
    r"(?:第\s*\d+\s*步)|然后|之后|首先)",
    re.IGNORECASE,
)

_STEP_LINE_RE = re.compile(r"^\s*(?:step\s*(\d+)|(\d+))\s*[.):、：]\s*(.+)$", re.IGNORECASE)


def _key_terms(text: str) -> list[str]:
    """The distinctive words and runs a claim is made of.

    English words of four letters or more, and runs of two or more CJK characters. Terms
    are matched as substrings rather than as tokens, because CJK does not insert the
    spaces a tokeniser would need, and a run inside a longer run is still the same words.
    """

    terms: list[str] = []
    for match in _WORD_RE.finditer(text or ""):
        word = match.group(0).lower()
        if word not in _STOPWORDS and word not in terms:
            terms.append(word)
    for match in _CJK_RE.finditer(text or ""):
        run = match.group(0)
        if run not in terms:
            terms.append(run)
    return terms


def _covered(wording: str, material: str, *, minimum: float) -> bool:
    terms = _key_terms(wording)
    if not terms:
        return True
    if not material.strip():
        return False
    found = sum(1 for term in terms if term in material)
    return found / len(terms) >= minimum


def _missing_qualifiers(candidate: ClaimCandidate, material: str) -> list[str]:
    wording = "\n".join([candidate.statement, *candidate.conditions])
    return [
        token
        for token, pattern in QUALIFIER_PATTERNS
        if pattern.search(material) and not pattern.search(wording)
    ]


def _negation_near(text: str, term: str, window: int = NEGATION_WINDOW) -> bool:
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        left = max(0, index - window)
        right = min(len(text), index + len(term) + window)
        if NEGATION_RE.search(text[left:right]):
            return True
        start = index + len(term)


def _denial(candidate: ClaimCandidate, material: str) -> str:
    """One sentence from the material that denies the candidate, if there is one.

    A chunk that merely discusses the topic is not support, and a chunk that denies the
    conclusion is not support either. A term the candidate itself hedges is skipped, so a
    statement that carries its own negation is not read as its own contradiction.
    """

    for term in _key_terms(candidate.statement):
        if _negation_near(candidate.statement, term):
            continue
        for sentence in re.split(r"(?<=[.!?。！？\n])\s*", material):
            if term not in sentence:
                continue
            if _negation_near(sentence, term):
                return sentence.strip()
    return ""


def _ordered_steps(text: str) -> list[tuple[int, str]]:
    """The ordered steps a material states, in the order it states them."""

    steps: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        match = _STEP_LINE_RE.match(line)
        if not match:
            continue
        steps.append((int(match.group(1) or match.group(2)), match.group(3).strip()))
    return steps
