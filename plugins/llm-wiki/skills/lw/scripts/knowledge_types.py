"""Data contracts for the v2 knowledge layer.

One question this module answers, once, for every caller. What combinations of
knowledge kind, derivation, epistemic status, lifecycle status, grounding status,
decision state and question state are legal, and what evidence does each of them
require. Scattered copies of that rule are how "the model proposed it" turns into
"the project adopted it", so the rule lives here as pure functions over plain
values and everything else calls in.

Standard library only. The same bytes ship as the canonical module and as the
vendored copy inside the skill package, so this file imports no sibling module by
package name; `knowledge_types` is the root of the v2 import graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 2

KNOWLEDGE_KINDS = (
    "fact",
    "definition",
    "distinction",
    "judgment",
    "decision",
    "rationale",
    "constraint",
    "process",
    "architecture",
    "open_question",
)

DERIVATIONS = ("explicit", "synthesized")

EPISTEMIC_STATUSES = ("asserted", "hypothesis", "verified", "disputed", "not_applicable")

LIFECYCLE_STATUSES = ("active", "superseded", "retracted")

GROUNDING_STATUSES = ("grounded", "needs_revalidation", "unsupported")

DECISION_STATES = ("proposed", "adopted", "rejected")

QUESTION_STATES = ("open", "resolved", "deferred")

RELATION_TYPES = ("supports", "derived_from", "refines", "supersedes", "contradicts", "depends_on")

RELATION_STATUSES = ("proposed", "accepted", "retracted")

ORIGIN_STATUSES = ("active", "invalidated", "retracted")

VALUE_GATE_DECISIONS = ("KEEP", "REVIEW", "DROP")

VALUE_GATE_REASON_CODES = (
    "reusable_decision",
    "reusable_constraint",
    "explains_choice",
    "reproducible_process",
    "important_distinction",
    "active_question",
    "supporting_context",
    "ephemeral_activity",
    "generic_background",
    "insufficient_context",
    "ambiguous_adoption",
    "ambiguous_identity",
)

REVIEW_ACTIONS = (
    "retain",
    "edit",
    "reject",
    "adopt_decision",
    "confirm_supersession",
    "confirm_identity",
)

RUN_STATES = (
    "received",
    "preparing",
    "extracting",
    "validating",
    "ready_to_commit",
    "failed",
    "committed",
    "projecting",
    "completed",
    "committed_projection_pending",
)

# The only legal moves between run states. `failed` is reachable from every
# in-flight state and terminal from all of them, which is what keeps a crashed
# extract from ever reading as completed.
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "received": frozenset({"preparing", "failed"}),
    "preparing": frozenset({"extracting", "failed"}),
    "extracting": frozenset({"validating", "failed"}),
    "validating": frozenset({"ready_to_commit", "failed"}),
    "ready_to_commit": frozenset({"committed", "failed"}),
    "committed": frozenset({"projecting", "committed_projection_pending", "failed"}),
    "projecting": frozenset({"completed", "committed_projection_pending", "failed"}),
    "committed_projection_pending": frozenset({"projecting", "completed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}

TERMINAL_RUN_STATES = frozenset({"completed", "failed"})

EVIDENCE_ERROR_CODES = (
    "EVIDENCE_NOT_FOUND",
    "SCOPE_MISMATCH",
    "HASH_MISMATCH",
    "SOURCE_WITHDRAWN",
    "RAW_UNAVAILABLE",
)

OFFSET_UNIT = "unicode_code_point"

PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
SPACE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_DIGEST = r"[0-9a-f]{32}(?:[0-9a-f]{32})?"
"""32 or 64 hex characters.

A store-assigned object gets a uuid4 hex, which is 32. A content-derived address
gets a full SHA-256 digest, which is 64. Both are permanent once issued, so the
pattern accepts both rather than forcing one of them to change width and
invalidate every citation already written.
"""

CLAIM_ID_PATTERN = re.compile(rf"^clm_{_DIGEST}$")
CLAIM_VERSION_PATTERN = re.compile(rf"^clv_{_DIGEST}$")
ORIGIN_ID_PATTERN = re.compile(rf"^org_{_DIGEST}$")
TOPIC_ID_PATTERN = re.compile(rf"^top_{_DIGEST}$")
EVIDENCE_ID_PATTERN = re.compile(rf"^evd_{_DIGEST}$")
ARTIFACT_ID_PATTERN = re.compile(rf"^art_{_DIGEST}$")
REVISION_ID_PATTERN = re.compile(rf"^rev_{_DIGEST}$")
SOURCE_ID_PATTERN = re.compile(rf"^src_{_DIGEST}$")
SUPPORT_GROUP_PATTERN = re.compile(rf"^sgr_{_DIGEST}$")
RELATION_ID_PATTERN = re.compile(rf"^rel_{_DIGEST}$")
PAGE_ID_PATTERN = re.compile(rf"^pag_{_DIGEST}$")


class KnowledgeError(ValueError):
    """A contract violation, carrying a stable machine-readable code."""

    def __init__(self, message: str, *, code: str = "INVALID_KNOWLEDGE"):
        super().__init__(message)
        self.code = code


class EvidenceError(KnowledgeError):
    """An evidence reference that cannot be resolved to exact source text."""

    def __init__(self, message: str, *, code: str):
        if code not in EVIDENCE_ERROR_CODES:
            raise KnowledgeError(f"Unknown evidence error code: {code!r}")
        super().__init__(message, code=code)


def _one_of(value: Any, allowed: Sequence[str], field_name: str, *, allow_null: bool = False) -> str | None:
    if value is None and allow_null:
        return None
    if value not in allowed:
        raise KnowledgeError(
            f"{field_name} must be one of {', '.join(allowed)}; got {value!r}.",
            code="INVALID_STATE",
        )
    return value


def _identifier(value: Any, pattern: re.Pattern[str], field_name: str) -> str:
    text = str(value or "")
    if not pattern.fullmatch(text):
        raise KnowledgeError(f"{field_name} is not a well-formed identifier: {value!r}.", code="INVALID_ID")
    return text


def _text(value: Any, field_name: str, *, max_chars: int, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise KnowledgeError(f"{field_name} cannot be empty.", code="INVALID_TEXT")
    if len(result) > max_chars:
        raise KnowledgeError(f"{field_name} exceeds the {max_chars} character limit.", code="INVALID_TEXT")
    return result


def _text_list(value: Any, field_name: str, *, max_items: int = 200, max_chars: int = 2_000) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise KnowledgeError(f"{field_name} must be a list.", code="INVALID_TEXT")
    if len(value) > max_items:
        raise KnowledgeError(f"{field_name} exceeds the {max_items} item limit.", code="INVALID_TEXT")
    result: list[str] = []
    for item in value:
        cleaned = _text(item, field_name, max_chars=max_chars)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


@dataclass(frozen=True)
class Scope:
    """Where a piece of knowledge belongs.

    A claim always carries a project. Topics are the one object that is shared
    across projects inside a knowledge space, which is why they carry the space
    and not the project.
    """

    knowledge_space_id: str
    project_id: str

    def __post_init__(self) -> None:
        if not SPACE_ID_PATTERN.fullmatch(str(self.knowledge_space_id or "")):
            raise KnowledgeError(
                "knowledge_space_id must be 1-64 lowercase letters, digits, underscores, or hyphens.",
                code="INVALID_SCOPE",
            )
        if not PROJECT_ID_PATTERN.fullmatch(str(self.project_id or "")):
            raise KnowledgeError(
                "project_id must be 1-64 lowercase letters, digits, underscores, or hyphens.",
                code="INVALID_SCOPE",
            )

    @classmethod
    def of(cls, knowledge_space_id: str, project_id: str) -> "Scope":
        return cls(str(knowledge_space_id).strip().lower(), str(project_id).strip().lower())

    def as_dict(self) -> dict[str, str]:
        return {"knowledge_space_id": self.knowledge_space_id, "project_id": self.project_id}


@dataclass(frozen=True)
class Attribution:
    """Who said it, and when they said it.

    `asserted_at` is the time the material states, recorded with its precision.
    It is deliberately allowed to be unknown rather than defaulted to the import
    time, because a decision imported today must not silently become today's.
    """

    asserted_by: str = "unknown"
    asserted_at: str | None = None
    asserted_at_precision: str = "unknown"

    @classmethod
    def of(
        cls,
        asserted_by: Any = "unknown",
        asserted_at: Any = None,
        asserted_at_precision: Any = "unknown",
    ) -> "Attribution":
        precision = _one_of(
            str(asserted_at_precision or "unknown"),
            ("day", "minute", "second", "unknown"),
            "asserted_at_precision",
        )
        when = str(asserted_at).strip() if asserted_at else None
        if when is None:
            precision = "unknown"
        return cls(
            asserted_by=_text(asserted_by, "asserted_by", max_chars=240, required=False) or "unknown",
            asserted_at=when,
            asserted_at_precision=precision or "unknown",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asserted_by": self.asserted_by,
            "asserted_at": self.asserted_at,
            "asserted_at_precision": self.asserted_at_precision,
        }


@dataclass(frozen=True)
class ClaimState:
    """The orthogonal status axes of one claim version.

    None of these is a confidence score. `epistemic_status` says what the
    material expressed, `grounding_status` says whether the current wording is
    still backed by a usable support chain, `lifecycle_status` says whether the
    claim is still the current knowledge, and `decision_state` is a separate axis
    that only a decision uses.
    """

    knowledge_kind: str
    derivation: str
    epistemic_status: str
    lifecycle_status: str = "active"
    grounding_status: str = "grounded"
    decision_state: str | None = None
    question_state: str | None = None

    def __post_init__(self) -> None:
        _one_of(self.knowledge_kind, KNOWLEDGE_KINDS, "knowledge_kind")
        _one_of(self.derivation, DERIVATIONS, "derivation")
        _one_of(self.epistemic_status, EPISTEMIC_STATUSES, "epistemic_status")
        _one_of(self.lifecycle_status, LIFECYCLE_STATUSES, "lifecycle_status")
        _one_of(self.grounding_status, GROUNDING_STATUSES, "grounding_status")
        _one_of(self.decision_state, DECISION_STATES, "decision_state", allow_null=True)
        _one_of(self.question_state, QUESTION_STATES, "question_state", allow_null=True)
        self._check_axis_membership()

    def _check_axis_membership(self) -> None:
        """Two axes belong to exactly one kind, and saying otherwise is a lie."""

        if self.decision_state is not None and self.knowledge_kind != "decision":
            raise KnowledgeError(
                f"decision_state applies to decision claims only; got kind {self.knowledge_kind!r}.",
                code="INVALID_STATE",
            )
        if self.question_state is not None and self.knowledge_kind != "open_question":
            raise KnowledgeError(
                f"question_state applies to open_question claims only; got kind {self.knowledge_kind!r}.",
                code="INVALID_STATE",
            )
        if self.knowledge_kind == "decision" and self.epistemic_status == "not_applicable":
            raise KnowledgeError(
                "A decision carries an epistemic status; use not_applicable only for definitions and open questions.",
                code="INVALID_STATE",
            )

    def required_support(self) -> list[str]:
        """What a caller must be able to prove before this state may be stored.

        Returns the names of the proofs this state demands. An empty list means
        the wording alone is enough, which is the case for a hypothesis or a
        proposed decision: those are faithful records of what the material said.
        """

        needed: list[str] = []
        if self.epistemic_status == "verified":
            needed.append("verification_record")
        if self.epistemic_status == "disputed":
            needed.append("dispute_record")
        if self.decision_state == "adopted":
            needed.append("adoption_record")
        if self.question_state == "resolved":
            needed.append("resolution_record")
        return needed

    def as_dict(self) -> dict[str, Any]:
        return {
            "knowledge_kind": self.knowledge_kind,
            "derivation": self.derivation,
            "epistemic_status": self.epistemic_status,
            "lifecycle_status": self.lifecycle_status,
            "grounding_status": self.grounding_status,
            "decision_state": self.decision_state,
            "question_state": self.question_state,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ClaimState":
        return cls(
            knowledge_kind=value["knowledge_kind"],
            derivation=value["derivation"],
            epistemic_status=value["epistemic_status"],
            lifecycle_status=value.get("lifecycle_status", "active"),
            grounding_status=value.get("grounding_status", "grounded"),
            decision_state=value.get("decision_state"),
            question_state=value.get("question_state"),
        )


def validate_claim_state(state: ClaimState, available: Iterable[str]) -> ClaimState:
    """Reject a state whose proofs are missing, and never soften it instead.

    The failure mode this prevents is the one that matters most: a model returns
    `adopted` for something the material only proposed, and a lenient validator
    downgrades it silently to `proposed`. Downgrading hides that the model tried,
    so this raises and the caller decides whether to re-ask or to drop the
    candidate. A caller that wants `proposed` must say `proposed`.
    """

    present = set(available)
    missing = [name for name in state.required_support() if name not in present]
    if missing:
        raise KnowledgeError(
            f"Claim state {state.epistemic_status}/{state.decision_state} requires {', '.join(missing)}.",
            code="MISSING_SUPPORT",
        )
    return state


@dataclass(frozen=True)
class ClaimVersionState:
    """One immutable snapshot of a claim's wording plus its status axes."""

    claim_version_id: str
    claim_id: str
    statement: str
    state: ClaimState
    conditions: tuple[str, ...] = ()
    attribution: Attribution = field(default_factory=Attribution)
    valid_from: str | None = None
    valid_to: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.claim_version_id, CLAIM_VERSION_PATTERN, "claim_version_id")
        _identifier(self.claim_id, CLAIM_ID_PATTERN, "claim_id")
        _text(self.statement, "statement", max_chars=20_000)
        validate_attributes(self.state.knowledge_kind, self.attributes)
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise KnowledgeError("valid_to precedes valid_from.", code="INVALID_STATE")


def validate_attributes(knowledge_kind: str, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the kind-specific payload, and keep unknown keys out of the claim.

    A process without its steps, or an architecture note without its components,
    cannot be reproduced from the claim alone, so the payload is part of the
    contract rather than free-form decoration.
    """

    payload = dict(attributes or {})
    if not isinstance(attributes, Mapping):
        raise KnowledgeError("attributes must be an object.", code="INVALID_ATTRIBUTES")
    if knowledge_kind == "process":
        steps = payload.get("steps", [])
        if not isinstance(steps, (list, tuple)):
            raise KnowledgeError("process.steps must be a list.", code="INVALID_ATTRIBUTES")
        cleaned = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                raise KnowledgeError("Each process step must be an object.", code="INVALID_ATTRIBUTES")
            cleaned.append({
                "order": int(step.get("order", index)),
                "action": _text(step.get("action"), "process step action", max_chars=2_000),
                "inputs": _text_list(step.get("inputs"), "process step inputs"),
                "outputs": _text_list(step.get("outputs"), "process step outputs"),
                "preconditions": _text_list(step.get("preconditions"), "process step preconditions"),
                "exceptions": _text_list(step.get("exceptions"), "process step exceptions"),
                "depends_on": [int(value) for value in step.get("depends_on", []) or []],
            })
        orders = [step["order"] for step in cleaned]
        if len(set(orders)) != len(orders):
            raise KnowledgeError("process step order values must be unique.", code="INVALID_ATTRIBUTES")
        for step in cleaned:
            unknown = [value for value in step["depends_on"] if value not in set(orders)]
            if unknown:
                raise KnowledgeError(
                    f"process step depends on unknown order(s): {unknown}.", code="INVALID_ATTRIBUTES"
                )
        return {"steps": cleaned}
    if knowledge_kind == "decision":
        adopted_by = _text_list(payload.get("adopted_by"), "decision.adopted_by", max_items=20, max_chars=240)
        return {"adopted_by": adopted_by} if adopted_by else {}
    if knowledge_kind == "architecture":
        components = _text_list(payload.get("components"), "architecture.components")
        return {"components": components} if components else {}
    if payload:
        raise KnowledgeError(
            f"attributes are not defined for knowledge kind {knowledge_kind!r}.", code="INVALID_ATTRIBUTES"
        )
    return {}


@dataclass(frozen=True)
class EvidenceSpan:
    """A half-open range over the saved normalized text of one parsed artifact."""

    artifact_id: str
    start: int
    end: int
    span_sha256: str
    offset_unit: str = OFFSET_UNIT

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, ARTIFACT_ID_PATTERN, "artifact_id")
        if self.offset_unit != OFFSET_UNIT:
            raise KnowledgeError(
                f"offset_unit must be {OFFSET_UNIT}; got {self.offset_unit!r}.", code="INVALID_SPAN"
            )
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise KnowledgeError("Span offsets must be integers.", code="INVALID_SPAN")
        if self.start < 0 or self.end <= self.start:
            raise KnowledgeError(
                f"Span must satisfy 0 <= start < end; got [{self.start}, {self.end}).", code="INVALID_SPAN"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.span_sha256 or "")):
            raise KnowledgeError("span_sha256 must be a sha256: digest.", code="INVALID_SPAN")

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "start": self.start,
            "end": self.end,
            "span_sha256": self.span_sha256,
            "offset_unit": self.offset_unit,
        }


@dataclass(frozen=True)
class ClaimOrigin:
    """One way a claim's wording was arrived at.

    A claim accumulates origins instead of overwriting one. The same statement
    synthesized from two pieces of evidence and later spoken outright by a user
    keeps both records, because "the system inferred this" and "the user said
    this" are different facts about the same sentence.
    """

    origin_id: str
    claim_version_id: str
    derivation: str
    evidence_refs: tuple[str, ...] = ()
    premise_claim_version_ids: tuple[str, ...] = ()
    inference_note: str = ""
    assumptions: tuple[str, ...] = ()
    support_group_id: str = ""
    origin_status: str = "active"
    attribution: Attribution = field(default_factory=Attribution)

    def __post_init__(self) -> None:
        _identifier(self.origin_id, ORIGIN_ID_PATTERN, "origin_id")
        _identifier(self.claim_version_id, CLAIM_VERSION_PATTERN, "claim_version_id")
        _one_of(self.derivation, DERIVATIONS, "derivation")
        _one_of(self.origin_status, ORIGIN_STATUSES, "origin_status")
        if self.support_group_id:
            _identifier(self.support_group_id, SUPPORT_GROUP_PATTERN, "support_group_id")
        for value in self.evidence_refs:
            _identifier(value, EVIDENCE_ID_PATTERN, "evidence_ref")
        for value in self.premise_claim_version_ids:
            _identifier(value, CLAIM_VERSION_PATTERN, "premise_claim_version_id")
        if self.derivation == "explicit":
            if not self.evidence_refs:
                raise KnowledgeError(
                    "An explicit origin needs at least one evidence reference.", code="MISSING_EVIDENCE"
                )
            if self.premise_claim_version_ids:
                raise KnowledgeError(
                    "An explicit origin derives from material, not from other claims.", code="INVALID_ORIGIN"
                )
            return
        if not self.evidence_refs and not self.premise_claim_version_ids:
            raise KnowledgeError(
                "A synthesized origin needs evidence or premise claims.", code="MISSING_EVIDENCE"
            )
        if not self.inference_note.strip():
            raise KnowledgeError(
                "A synthesized origin must state its inference in a reviewable note.", code="MISSING_INFERENCE_NOTE"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin_id": self.origin_id,
            "claim_version_id": self.claim_version_id,
            "derivation": self.derivation,
            "evidence_refs": list(self.evidence_refs),
            "premise_claim_version_ids": list(self.premise_claim_version_ids),
            "inference_note": self.inference_note,
            "assumptions": list(self.assumptions),
            "support_group_id": self.support_group_id,
            "origin_status": self.origin_status,
            "attribution": self.attribution.as_dict(),
        }


@dataclass(frozen=True)
class SupportGroup:
    """A conjunction of requirements. Groups are alternatives to each other.

    Group G1 holding A AND B and group G2 holding D means the claim stands while
    any one group is whole. That is why a withdrawn source inside G1 marks the
    claim for revalidation rather than deleting it: G2 may still carry it.
    """

    support_group_id: str
    claim_version_id: str
    requirement_ids: tuple[str, ...]
    derived_from_claims: bool = False

    def __post_init__(self) -> None:
        _identifier(self.support_group_id, SUPPORT_GROUP_PATTERN, "support_group_id")
        _identifier(self.claim_version_id, CLAIM_VERSION_PATTERN, "claim_version_id")
        if not self.requirement_ids:
            raise KnowledgeError("A support group needs at least one requirement.", code="MISSING_EVIDENCE")

    def as_dict(self) -> dict[str, Any]:
        return {
            "support_group_id": self.support_group_id,
            "claim_version_id": self.claim_version_id,
            "requirement_ids": list(self.requirement_ids),
            "derived_from_claims": self.derived_from_claims,
        }


def support_group_outcome(groups: Sequence[Sequence[bool]]) -> str:
    """Collapse per-requirement availability into one grounding outcome.

    `groups` carries one boolean per requirement, in the same order the group was
    recorded. Whole group means every requirement is still available.
    """

    if not groups:
        return "unsupported"
    if any(all(group) for group in groups):
        return "grounded"
    if any(any(group) for group in groups):
        return "needs_revalidation"
    return "unsupported"


@dataclass(frozen=True)
class ClaimRelation:
    """A typed, directed edge between two claim versions.

    Endpoint order is part of the meaning, so each relation type states which end
    is which. Storing both endpoints in untyped `source_id`/`target_id` columns is
    what lets a `refines` edge silently become a `supersedes` one.
    """

    relation_id: str
    relation_type: str
    from_claim_version_id: str
    to_claim_version_id: str
    relation_status: str = "proposed"
    origin_evidence_refs: tuple[str, ...] = ()
    recorded_by: str = "system"

    def __post_init__(self) -> None:
        _identifier(self.relation_id, RELATION_ID_PATTERN, "relation_id")
        _one_of(self.relation_type, RELATION_TYPES, "relation_type")
        _one_of(self.relation_status, RELATION_STATUSES, "relation_status")
        _identifier(self.from_claim_version_id, CLAIM_VERSION_PATTERN, "from_claim_version_id")
        _identifier(self.to_claim_version_id, CLAIM_VERSION_PATTERN, "to_claim_version_id")
        if self.from_claim_version_id == self.to_claim_version_id:
            raise KnowledgeError(
                "A claim cannot relate to itself.", code="INVALID_RELATION"
            )
        for value in self.origin_evidence_refs:
            _identifier(value, EVIDENCE_ID_PATTERN, "origin_evidence_ref")

    def canonical_endpoints(self) -> tuple[str, str]:
        """`contradicts` is semantically symmetric, so it stores one orientation."""

        if self.relation_type == "contradicts":
            return tuple(sorted((self.from_claim_version_id, self.to_claim_version_id)))  # type: ignore[return-value]
        return (self.from_claim_version_id, self.to_claim_version_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "relation_type": self.relation_type,
            "from_claim_version_id": self.from_claim_version_id,
            "to_claim_version_id": self.to_claim_version_id,
            "relation_status": self.relation_status,
            "origin_evidence_refs": list(self.origin_evidence_refs),
            "recorded_by": self.recorded_by,
        }


derive_from_direction = {
    "supports": "premise_to_supported",
    "derived_from": "derived_to_premise",
    "refines": "specific_to_general",
    "supersedes": "replacement_to_replaced",
    "contradicts": "symmetric",
    "depends_on": "dependent_to_dependency",
}
"""Which end of each relation type means what. A reader checking a stored edge
reads this table instead of re-deriving the direction from the relation name."""


def find_derivation_cycle(
    nodes: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> list[str] | None:
    """Return one cycle in the `derived_from` graph, or None when it is acyclic.

    Derivation is a proof obligation, so a cycle in it means a claim is part of
    its own justification. `edges` run from a derived claim version to a premise
    claim version, matching the stored direction. Iterative depth-first with a
    grey/black marking rather than recursion, because a long synthesis chain
    should not be able to exhaust the interpreter when a malformed one arrives.
    """

    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
    state: dict[str, int] = {}
    for root in nodes:
        if state.get(root) == 2:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, index = stack[-1]
            if index == 0:
                state[node] = 1
                path.append(node)
            neighbours = adjacency.get(node, [])
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                neighbour = neighbours[index]
                mark = state.get(neighbour, 0)
                if mark == 1:
                    return [*path[path.index(neighbour):], neighbour]
                if mark == 0:
                    stack.append((neighbour, 0))
                continue
            state[node] = 2
            path.pop()
            stack.pop()
    return None


def validate_relation_endpoint_kinds(relation_type: str, from_kind: str, to_kind: str) -> None:
    """Reject edges whose meaning the graph cannot carry.

    Both endpoints are claim versions by construction, which is what keeps a
    topic out of a claim-to-claim edge. What is left to check is the pair.
    """

    _one_of(relation_type, RELATION_TYPES, "relation_type")
    for kind in (from_kind, to_kind):
        _one_of(kind, KNOWLEDGE_KINDS, "knowledge_kind")
    if relation_type == "supersedes" and "open_question" in (from_kind, to_kind):
        raise KnowledgeError(
            "supersedes replaces an established judgment, not an open question.", code="INVALID_RELATION"
        )


@dataclass(frozen=True)
class ClaimCandidate:
    """A discovery-stage proposal. Nothing here is committed knowledge yet."""

    statement: str
    state: ClaimState
    subjects: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    attribution: Attribution = field(default_factory=Attribution)
    evidence_refs: tuple[str, ...] = ()
    premise_claim_version_ids: tuple[str, ...] = ()
    inference_note: str = ""
    assumptions: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    disposition: str = ""
    reason_codes: tuple[str, ...] = ()
    batch_id: str = ""

    def __post_init__(self) -> None:
        _text(self.statement, "statement", max_chars=20_000)
        for value in self.evidence_refs:
            _identifier(value, EVIDENCE_ID_PATTERN, "evidence_ref")
        for value in self.premise_claim_version_ids:
            _identifier(value, CLAIM_VERSION_PATTERN, "premise_claim_version_id")
        if self.disposition:
            _one_of(self.disposition, VALUE_GATE_DECISIONS, "disposition")
        for code in self.reason_codes:
            _one_of(code, VALUE_GATE_REASON_CODES, "reason_code")

    @property
    def is_keep(self) -> bool:
        return self.disposition == "KEEP"


def build_change_set(
    *,
    knowledge_space_id: str,
    project_id: str,
    run_id: str,
    claims: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]] = (),
    topics: Sequence[Mapping[str, Any]] = (),
    reviews: Sequence[Mapping[str, Any]] = (),
    dropped: Sequence[Mapping[str, Any]] = (),
    base_version: int = 0,
) -> dict[str, Any]:
    """Assemble the one object a commit consumes, with its scope on its face.

    Everything the commit needs travels in this structure, so the store never has
    to re-derive intent from the pieces. `base_version` rides along because a
    change set built against version 7 must not be applied to version 9.
    """

    if not isinstance(base_version, int) or base_version < 0:
        raise KnowledgeError("base_version must be a non-negative integer.", code="INVALID_VERSION")
    _text(run_id, "run_id", max_chars=120)
    return {
        "schema_version": SCHEMA_VERSION,
        "knowledge_space_id": knowledge_space_id,
        "project_id": project_id,
        "run_id": run_id,
        "base_version": base_version,
        "claims": [dict(claim) for claim in claims],
        "relations": [dict(relation) for relation in relations],
        "topics": [dict(topic) for topic in topics],
        "reviews": [dict(review) for review in reviews],
        "dropped": [dict(item) for item in dropped],
    }
