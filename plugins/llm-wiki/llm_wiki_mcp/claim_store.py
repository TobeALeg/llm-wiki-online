"""Durable, project-scoped storage for the v2 knowledge layer.

Claims are the authoritative state. This module owns the one transaction that
creates them, their versions, their origins, their evidence links, their
relations, their review queue entries and the projection dirty marks, so a
half-committed knowledge change cannot exist.

Two things a caller never supplies: an object id and a timestamp. The store
mints both. That is what stops a model from naming itself the author of a claim
or from backdating a decision to a time it was never recorded at.

Standard library only, and no sibling import by package name, so the same bytes
work as the canonical module and as the vendored copy inside the skill package.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .evidence import (
        Artifact,
        EvidenceError,
        EvidenceRecord,
        RecoveredEvidence,
        SourceRevision,
        recover,
    )
    from .knowledge_types import (
        CLAIM_ID_PATTERN,
        ClaimCandidate,
        ClaimRelation,
        ClaimState,
        ClaimVersionState,
        KnowledgeError,
        REVIEW_ACTIONS,
        RELATION_STATUSES,
        RUN_TRANSITIONS,
        Scope,
        TERMINAL_RUN_STATES,
        VALUE_GATE_DECISIONS,
        build_change_set,
        find_derivation_cycle,
        support_group_outcome,
        validate_claim_state,
        validate_relation_endpoint_kinds,
    )
except ImportError:  # pragma: no cover - the packaged layout
    from evidence import (  # type: ignore[no-redef]
        Artifact,
        EvidenceError,
        EvidenceRecord,
        RecoveredEvidence,
        SourceRevision,
        recover,
    )
    from knowledge_types import (  # type: ignore[no-redef]
        CLAIM_ID_PATTERN,
        ClaimCandidate,
        ClaimRelation,
        ClaimState,
        ClaimVersionState,
        KnowledgeError,
        REVIEW_ACTIONS,
        RELATION_STATUSES,
        RUN_TRANSITIONS,
        Scope,
        TERMINAL_RUN_STATES,
        VALUE_GATE_DECISIONS,
        build_change_set,
        find_derivation_cycle,
        support_group_outcome,
        validate_claim_state,
        validate_relation_endpoint_kinds,
    )

DEFAULT_KNOWLEDGE_SPACE = "local"
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ACTOR_LIMIT = 240

V2_TABLES = (
    "dispositions",
    "knowledge_projects",
    "knowledge_submissions",
    "sources",
    "source_revisions",
    "parsed_artifacts",
    "evidence_refs",
    "rendered_chunks",
    "topics",
    "topic_aliases",
    "topic_claims",
    "claims",
    "claim_versions",
    "claim_subjects",
    "claim_origins",
    "claim_evidence",
    "claim_support_requirements",
    "claim_relations",
    "content_fingerprints",
    "page_projections",
    "page_claims",
    "page_topics",
    "page_redirects",
    "ingest_runs",
    "run_items",
    "stage_artifacts",
    "review_queue",
    "review_decisions",
    "knowledge_meta",
    "schema_migrations",
)
"""Every table the v2 layer creates. Named rather than inferred so a test can
assert the schema without reading SQL."""


class ClaimStoreError(RuntimeError):
    """A storage or request-consistency failure in the v2 layer."""


class IdempotencyError(ClaimStoreError):
    """The same key arrived with a different request. Never answer with the old result."""


class ConflictError(ClaimStoreError):
    def __init__(self, message: str, *, current_version: int):
        super().__init__(message)
        self.current_version = current_version


class ScopeError(ClaimStoreError):
    """A reference that crosses a project or knowledge-space boundary."""


class StaleReviewError(ClaimStoreError):
    """The reviewed object moved after the review was opened."""

    def __init__(self, message: str, *, current_version: str):
        super().__init__(message)
        self.current_version = current_version


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def statement_fingerprint(scope: Scope, statement: str, conditions: Sequence[str]) -> str:
    """A content fingerprint used to find a claim that may already say this.

    It is a candidate signal, never a merge decision. Two claims whose wording
    fingerprints the same can still differ in subject, project, conditions, time
    or modality, and merging on the fingerprint alone is what this value must not
    be used for.
    """

    normalized = unicodedata.normalize("NFKC", statement).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    payload = {
        "space": scope.knowledge_space_id,
        "project": scope.project_id,
        "statement": normalized,
        "conditions": sorted(conditions),
    }
    return "fp_" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CommitOutcome:
    """What one commit actually did, in the shape a release report reads."""

    run_id: str
    knowledge_version: int
    status: str
    created_claims: int
    updated_claims: int
    new_relations: int
    review_pending: int
    dropped_candidates: int
    projection_status: str
    created_claim_ids: tuple[str, ...]
    updated_claim_ids: tuple[str, ...]
    unchanged_claims: int = 0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A replayed commit is rebuilt from JSON, where these arrived as lists.
        # Normalising here is what makes a retry return a value equal to the first
        # answer rather than merely one that prints the same.
        object.__setattr__(self, "created_claim_ids", tuple(self.created_claim_ids))
        object.__setattr__(self, "updated_claim_ids", tuple(self.updated_claim_ids))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "knowledge_version": self.knowledge_version,
            "status": self.status,
            "created_claims": self.created_claims,
            "updated_claims": self.updated_claims,
            "new_relations": self.new_relations,
            "review_pending": self.review_pending,
            "dropped_candidates": self.dropped_candidates,
            "projection_status": self.projection_status,
            "created_claim_ids": list(self.created_claim_ids),
            "updated_claim_ids": list(self.updated_claim_ids),
            "unchanged_claims": self.unchanged_claims,
            "warnings": list(self.warnings),
        }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_projects (
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, project_id)
);

CREATE TABLE IF NOT EXISTS knowledge_submissions (
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    intent_hash TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, project_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    label TEXT NOT NULL,
    origin_uri TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    UNIQUE (knowledge_space_id, project_id, source_id)
);

CREATE TABLE IF NOT EXISTS source_revisions (
    revision_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    raw_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source_time TEXT,
    raw_available INTEGER NOT NULL,
    storage_key TEXT,
    ordinal INTEGER NOT NULL DEFAULT 1,
    withdrawn_at TEXT
);
CREATE INDEX IF NOT EXISTS source_revisions_source ON source_revisions(source_id, ordinal);

CREATE TABLE IF NOT EXISTS parsed_artifacts (
    artifact_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES source_revisions(revision_id) ON DELETE CASCADE,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    structure_json TEXT NOT NULL,
    parse_quality TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (knowledge_space_id, project_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS parsed_artifacts_revision ON parsed_artifacts(revision_id);

CREATE TABLE IF NOT EXISTS evidence_refs (
    evidence_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES parsed_artifacts(artifact_id) ON DELETE RESTRICT,
    spans_json TEXT NOT NULL,
    span_hashes_json TEXT NOT NULL,
    heading_path_json TEXT NOT NULL,
    context_refs_json TEXT NOT NULL,
    offset_unit TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    UNIQUE (knowledge_space_id, project_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS evidence_refs_artifact ON evidence_refs(artifact_id);

CREATE TABLE IF NOT EXISTS rendered_chunks (
    chunk_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES parsed_artifacts(artifact_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL DEFAULT '',
    chunk_index INTEGER NOT NULL,
    evidence_spans_json TEXT NOT NULL,
    context_spans_json TEXT NOT NULL,
    render_recipe TEXT NOT NULL,
    render_recipe_version TEXT NOT NULL,
    text TEXT NOT NULL,
    verbatim INTEGER NOT NULL,
    heading_path_json TEXT NOT NULL,
    starting_line INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (artifact_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    canonical_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    merged_into_topic_id TEXT REFERENCES topics(topic_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS topic_aliases (
    alias_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_aliases_alias ON topic_aliases(alias);

CREATE TABLE IF NOT EXISTS topic_claims (
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    topic_id TEXT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, project_id, topic_id, claim_id)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    current_version_id TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (knowledge_space_id, project_id, claim_id)
);
CREATE INDEX IF NOT EXISTS claims_fingerprint ON claims(project_id, fingerprint);
CREATE INDEX IF NOT EXISTS claims_project ON claims(knowledge_space_id, project_id, lifecycle_status);

CREATE TABLE IF NOT EXISTS claim_versions (
    claim_version_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    statement TEXT NOT NULL,
    knowledge_kind TEXT NOT NULL,
    derivation TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    grounding_status TEXT NOT NULL,
    decision_state TEXT,
    question_state TEXT,
    conditions_json TEXT NOT NULL,
    asserted_by TEXT NOT NULL,
    asserted_at TEXT,
    asserted_at_precision TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    attributes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    knowledge_version INTEGER NOT NULL,
    UNIQUE (claim_id, version),
    UNIQUE (knowledge_space_id, project_id, claim_version_id)
);
CREATE INDEX IF NOT EXISTS claim_versions_claim ON claim_versions(claim_id, version DESC);

CREATE TABLE IF NOT EXISTS claim_subjects (
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (claim_version_id, subject)
);

CREATE TABLE IF NOT EXISTS claim_origins (
    origin_id TEXT PRIMARY KEY,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id) ON DELETE CASCADE,
    derivation TEXT NOT NULL,
    inference_note TEXT NOT NULL DEFAULT '',
    assumptions_json TEXT NOT NULL,
    support_group_id TEXT NOT NULL DEFAULT '',
    origin_status TEXT NOT NULL DEFAULT 'active',
    asserted_by TEXT NOT NULL DEFAULT 'unknown',
    asserted_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claim_origins_version ON claim_origins(claim_version_id);

CREATE TABLE IF NOT EXISTS claim_evidence (
    origin_id TEXT NOT NULL REFERENCES claim_origins(origin_id) ON DELETE CASCADE,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (origin_id, evidence_id),
    FOREIGN KEY (knowledge_space_id, project_id, evidence_id)
        REFERENCES evidence_refs (knowledge_space_id, project_id, evidence_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS claim_support_requirements (
    support_group_id TEXT NOT NULL,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id) ON DELETE CASCADE,
    requirement_kind TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (claim_version_id, support_group_id, requirement_kind, requirement_id)
);
CREATE INDEX IF NOT EXISTS support_requirements_version ON claim_support_requirements(claim_version_id);

CREATE TABLE IF NOT EXISTS claim_relations (
    relation_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    relation_status TEXT NOT NULL,
    from_claim_version_id TEXT NOT NULL,
    to_claim_version_id TEXT NOT NULL,
    from_claim_id TEXT NOT NULL,
    to_claim_id TEXT NOT NULL,
    origin_evidence_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (relation_type, from_claim_version_id, to_claim_version_id)
);
CREATE INDEX IF NOT EXISTS claim_relations_from ON claim_relations(from_claim_id);
CREATE INDEX IF NOT EXISTS claim_relations_to ON claim_relations(to_claim_id);

CREATE TABLE IF NOT EXISTS content_fingerprints (
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, project_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS page_projections (
    page_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    page_kind TEXT NOT NULL DEFAULT 'topic',
    renderer_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    markdown TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    projection_status TEXT NOT NULL DEFAULT 'current',
    dirty INTEGER NOT NULL DEFAULT 0,
    manual_edit_hash TEXT,
    rendered_at TEXT NOT NULL,
    UNIQUE (knowledge_space_id, project_id, slug)
);

CREATE TABLE IF NOT EXISTS page_claims (
    page_id TEXT NOT NULL REFERENCES page_projections(page_id) ON DELETE CASCADE,
    claim_version_id TEXT NOT NULL,
    section TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (page_id, section, position)
);

CREATE TABLE IF NOT EXISTS page_topics (
    page_id TEXT NOT NULL REFERENCES page_projections(page_id) ON DELETE CASCADE,
    topic_id TEXT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
    PRIMARY KEY (page_id, topic_id)
);

CREATE TABLE IF NOT EXISTS page_redirects (
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    old_slug TEXT NOT NULL,
    page_id TEXT NOT NULL REFERENCES page_projections(page_id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, project_id, old_slug)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    error_code TEXT,
    review_pending INTEGER NOT NULL DEFAULT 0,
    committed_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ingest_runs_project ON ingest_runs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS run_items (
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, batch_id)
);

CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    unit_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dispositions_project ON dispositions(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS stage_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    output_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, stage, input_fingerprint, prompt_version, model_id)
);

CREATE TABLE IF NOT EXISTS review_queue (
    review_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_version TEXT NOT NULL,
    question TEXT NOT NULL,
    trigger_code TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS review_queue_project ON review_queue(project_id, status, created_at);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_queue(review_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    expected_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (review_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS knowledge_meta (
    project_id TEXT PRIMARY KEY,
    knowledge_space_id TEXT NOT NULL,
    knowledge_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


def _as_bool(value: Any) -> int:
    return 1 if value else 0


class ClaimStore:
    """The v2 half of the Wiki database. One transaction per knowledge change."""

    def __init__(
        self,
        database: str | Path,
        *,
        knowledge_space_id: str = DEFAULT_KNOWLEDGE_SPACE,
    ):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.knowledge_space_id = knowledge_space_id
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # SQLite requires this per connection. A schema that writes REFERENCES is
        # not a schema that enforces them, so it is set and then checked.
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def foreign_keys_enabled(self) -> bool:
        with self._db() as db:
            return bool(db.execute("PRAGMA foreign_keys").fetchone()[0])

    def initialize(self) -> None:
        with self._db() as db:
            if not db.execute("PRAGMA foreign_keys").fetchone()[0]:
                raise ClaimStoreError("Foreign key enforcement is off on this connection.")
            db.executescript(SCHEMA_SQL)
            self._add_missing_columns(db)
            checksum = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (2, "knowledge_v2", checksum, now_iso()),
            )

    @staticmethod
    def _add_missing_columns(db: sqlite3.Connection) -> None:
        """Add a column a database created by an earlier build does not have.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
        column only reaches an older database through an explicit ALTER.
        """

        wanted = {"review_decisions": ("request_hash",), "knowledge_submissions": ("intent_hash",)}
        for table, columns in wanted.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )

    def schema_tables(self) -> set[str]:
        with self._db() as db:
            return {
                row["name"]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }

    def _scope(self, scope: Scope | None, project_id: str | None) -> Scope:
        if scope is not None:
            return scope
        if not project_id:
            raise ClaimStoreError("A scope or a project id is required.")
        return Scope.of(self.knowledge_space_id, project_id)

    def _ensure_meta(self, db: sqlite3.Connection, scope: Scope) -> int:
        row = db.execute(
            "SELECT knowledge_version FROM knowledge_meta WHERE project_id = ?", (scope.project_id,)
        ).fetchone()
        if row:
            return int(row["knowledge_version"])
        db.execute(
            "INSERT INTO knowledge_meta(project_id, knowledge_space_id, knowledge_version, updated_at) VALUES (?, ?, 0, ?)",
            (scope.project_id, scope.knowledge_space_id, now_iso()),
        )
        return 0

    def current_version(self, project_id: str) -> int:
        scope = Scope.of(self.knowledge_space_id, project_id)
        with self._db() as db:
            return self._ensure_meta(db, scope)

    # ------------------------------------------------------------------
    # Sources, revisions, artifacts, evidence
    # ------------------------------------------------------------------

    def freeze_revision(
        self,
        *,
        scope: Scope,
        source_id: str,
        source_type: str,
        label: str,
        raw_content: str,
        captured_at: str | None = None,
        source_time: str | None = None,
        raw_available: bool = True,
        origin_uri: str = "",
        storage_key: str | None = None,
    ) -> dict[str, Any]:
        """Record one capture of a source as a new immutable revision.

        New content for a source id never overwrites an old revision. The old one
        keeps its artifacts and citations, which is what lets a citation written
        last month still resolve after the source is edited.
        """

        captured = captured_at or now_iso()
        raw_sha = "sha256:" + hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT source_id FROM sources WHERE source_id = ? AND project_id = ?",
                    (source_id, scope.project_id),
                ).fetchone()
                if not existing:
                    db.execute(
                        """INSERT INTO sources(source_id, knowledge_space_id, project_id, source_type, label, origin_uri, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (source_id, scope.knowledge_space_id, scope.project_id, source_type, label, origin_uri, captured),
                    )
                prior = db.execute(
                    "SELECT revision_id, raw_sha256 FROM source_revisions WHERE source_id = ? ORDER BY ordinal DESC",
                    (source_id,),
                ).fetchall()
                if prior and prior[0]["raw_sha256"] == raw_sha:
                    db.commit()
                    return {
                        "revision_id": prior[0]["revision_id"],
                        "source_id": source_id,
                        "raw_sha256": raw_sha,
                        "captured_at": captured,
                        "raw_available": raw_available,
                        "source_time": source_time,
                        "storage_key": storage_key,
                        "reused": True,
                    }
                revision_id = new_id("rev_")
                db.execute(
                    """INSERT INTO source_revisions(revision_id, source_id, raw_sha256, captured_at, source_time, raw_available, storage_key, ordinal)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        revision_id,
                        source_id,
                        raw_sha,
                        captured,
                        source_time,
                        _as_bool(raw_available),
                        storage_key,
                        len(prior) + 1,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "revision_id": revision_id,
            "source_id": source_id,
            "raw_sha256": raw_sha,
            "captured_at": captured,
            "raw_available": raw_available,
            "source_time": source_time,
            "storage_key": storage_key,
            "reused": False,
        }

    def store_artifact(
        self,
        *,
        scope: Scope,
        artifact: Artifact,
        chunks: Sequence[Any] = (),
        batch_of: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Persist a frozen parse output and the chunks rendered from it."""

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT OR IGNORE INTO parsed_artifacts(
                           artifact_id, revision_id, knowledge_space_id, project_id, normalized_text,
                           normalized_sha256, parser_name, parser_version, config_hash, structure_json,
                           parse_quality, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact.artifact_id,
                        artifact.revision_id,
                        scope.knowledge_space_id,
                        scope.project_id,
                        artifact.normalized_text,
                        artifact.normalized_sha256,
                        artifact.parser_name,
                        artifact.parser_version,
                        artifact.config_hash,
                        json.dumps(artifact.structure, ensure_ascii=False, sort_keys=True),
                        artifact.parse_quality,
                        now_iso(),
                    ),
                )
                for chunk in chunks:
                    batch_id = (batch_of or {}).get(getattr(chunk, "chunk_id", ""), "")
                    db.execute(
                        """INSERT OR REPLACE INTO rendered_chunks(
                               chunk_id, artifact_id, batch_id, chunk_index, evidence_spans_json,
                               context_spans_json, render_recipe, render_recipe_version, text,
                               verbatim, heading_path_json, starting_line)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            chunk.chunk_id,
                            artifact.artifact_id,
                            batch_id,
                            chunk.index,
                            _json([list(span) for span in chunk.evidence()]),
                            _json([list(span) for span in chunk.context_spans]),
                            chunk.render_recipe,
                            "1",
                            chunk.text,
                            _as_bool(chunk.verbatim),
                            _json(list(chunk.heading_path)),
                            getattr(chunk, "starting_line", 1),
                        ),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"artifact_id": artifact.artifact_id, "chunks": len(chunks)}

    def register_evidence(self, record: EvidenceRecord, *, scope: Scope) -> str:
        """Store a citation, refusing one that names another project's artifact."""

        if record.project_id != scope.project_id:
            raise ScopeError(
                f"Evidence {record.evidence_id} names project {record.project_id}, not {scope.project_id}."
            )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                artifact = db.execute(
                    "SELECT artifact_id FROM parsed_artifacts WHERE artifact_id = ? AND project_id = ?",
                    (record.artifact_id, scope.project_id),
                ).fetchone()
                if not artifact:
                    raise ScopeError(
                        f"Artifact {record.artifact_id} is not registered in project {scope.project_id}."
                    )
                db.execute(
                    """INSERT OR IGNORE INTO evidence_refs(
                           evidence_id, knowledge_space_id, project_id, artifact_id, spans_json,
                           span_hashes_json, heading_path_json, context_refs_json, offset_unit, label, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.evidence_id,
                        scope.knowledge_space_id,
                        scope.project_id,
                        record.artifact_id,
                        _json([list(span) for span in record.spans]),
                        _json(list(record.span_hashes)),
                        _json(list(record.heading_path)),
                        _json(list(record.structural_context_refs)),
                        record.offset_unit,
                        record.label,
                        now_iso(),
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return record.evidence_id

    def withdraw_source(self, source_id: str, scope: Scope, *, withdrawn_at: str | None = None) -> dict[str, Any]:
        """Mark a source withdrawn and re-evaluate the claims that lean on it.

        The claims are not deleted and not called false. Their support is
        recomputed, so one withdrawn premise inside a conjunction moves the claim
        to needs_revalidation while an independent support group can still carry it.
        """

        stamp = withdrawn_at or now_iso()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "UPDATE sources SET withdrawn_at = ? WHERE source_id = ? AND project_id = ?",
                    (stamp, source_id, scope.project_id),
                )
                db.execute(
                    "UPDATE source_revisions SET withdrawn_at = ? WHERE source_id = ?",
                    (stamp, source_id),
                )
                db.execute(
                    """UPDATE evidence_refs SET withdrawn_at = ?
                       WHERE artifact_id IN (
                           SELECT a.artifact_id FROM parsed_artifacts a
                           JOIN source_revisions r ON r.revision_id = a.revision_id
                           WHERE r.source_id = ?
                       )""",
                    (stamp, source_id),
                )
                affected = self._recompute_support(db, scope)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"source_id": source_id, "withdrawn_at": stamp, "claims_recomputed": affected}

    def load_evidence(self, evidence_id: str, scope: Scope) -> RecoveredEvidence:
        """Resolve a citation, or fail with the code that says why it could not be."""

        with self._db() as db:
            row = db.execute(
                """SELECT e.*, a.normalized_text, a.normalized_sha256, a.parser_name, a.parser_version,
                          a.config_hash, a.structure_json, a.parse_quality, a.revision_id,
                          r.raw_available, r.withdrawn_at AS revision_withdrawn,
                          s.label AS source_label, s.withdrawn_at AS source_withdrawn
                   FROM evidence_refs e
                   JOIN parsed_artifacts a ON a.artifact_id = e.artifact_id
                   JOIN source_revisions r ON r.revision_id = a.revision_id
                   JOIN sources s ON s.source_id = r.source_id
                   WHERE e.evidence_id = ?""",
                (evidence_id,),
            ).fetchone()
        if not row:
            raise EvidenceError(f"Unknown evidence: {evidence_id}.", code="EVIDENCE_NOT_FOUND")
        if row["project_id"] != scope.project_id:
            raise EvidenceError(
                f"Evidence {evidence_id} belongs to project {row['project_id']}, not {scope.project_id}.",
                code="SCOPE_MISMATCH",
            )
        record = EvidenceRecord(
            evidence_id=row["evidence_id"],
            project_id=row["project_id"],
            artifact_id=row["artifact_id"],
            spans=tuple((int(a), int(b)) for a, b in json.loads(row["spans_json"])),
            span_hashes=tuple(json.loads(row["span_hashes_json"])),
            heading_path=tuple(json.loads(row["heading_path_json"])),
            structural_context_refs=tuple(json.loads(row["context_refs_json"])),
            offset_unit=row["offset_unit"],
            label=row["label"],
        )
        artifact = Artifact(
            artifact_id=row["artifact_id"],
            revision_id=row["revision_id"],
            normalized_text=row["normalized_text"],
            normalized_sha256=row["normalized_sha256"],
            parser_name=row["parser_name"],
            parser_version=row["parser_version"],
            config_hash=row["config_hash"],
            structure=json.loads(row["structure_json"]),
            parse_quality=row["parse_quality"],
        )
        if row["source_withdrawn"] or row["revision_withdrawn"]:
            raise EvidenceError(
                f"Evidence {evidence_id} names a withdrawn source.", code="SOURCE_WITHDRAWN"
            )
        return recover(
            record=record,
            artifact=artifact,
            expected_project_id=scope.project_id,
            source_withdrawn=False,
            raw_available=bool(row["raw_available"]),
            source_label=row["source_label"],
        )

    def source_status(self, source_id: str, scope: Scope) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE source_id = ? AND project_id = ?",
                (source_id, scope.project_id),
            ).fetchone()
            if not row:
                return {"source_id": source_id, "found": False}
            revisions = db.execute(
                "SELECT revision_id, ordinal, raw_sha256, raw_available, withdrawn_at FROM source_revisions WHERE source_id = ? ORDER BY ordinal",
                (source_id,),
            ).fetchall()
        return {
            "source_id": source_id,
            "found": True,
            "withdrawn_at": row["withdrawn_at"],
            "label": row["label"],
            "revisions": [
                {
                    "revision_id": item["revision_id"],
                    "ordinal": item["ordinal"],
                    "raw_sha256": item["raw_sha256"],
                    "raw_available": bool(item["raw_available"]),
                    "withdrawn_at": item["withdrawn_at"],
                }
                for item in revisions
            ],
        }

    def raw_available(self, source_id: str, scope: Scope) -> bool:
        status = self.source_status(source_id, scope)
        if not status["found"] or not status["revisions"]:
            return False
        return bool(status["revisions"][-1]["raw_available"])

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    def ensure_topic(
        self,
        *,
        scope: Scope,
        canonical_label: str,
        aliases: Sequence[str] = (),
        topic_id: str | None = None,
    ) -> str:
        """Register a topic identity, shared across projects in one knowledge space.

        The same alias may point at more than one topic. Forcing an alias to be
        unique is how two different things called "Harness" get merged into one.
        """

        label = canonical_label.strip()
        if not label:
            raise ClaimStoreError("A topic needs a canonical label.")
        identifier = topic_id or new_id("top_")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT OR IGNORE INTO topics(topic_id, knowledge_space_id, canonical_label, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                    (identifier, scope.knowledge_space_id, label, now_iso()),
                )
                for alias in {label, *[a.strip() for a in aliases if a.strip()]}:
                    duplicate = db.execute(
                        "SELECT alias_id FROM topic_aliases WHERE topic_id = ? AND alias = ? AND project_id = ?",
                        (identifier, alias, scope.project_id),
                    ).fetchone()
                    if not duplicate:
                        db.execute(
                            "INSERT INTO topic_aliases(alias_id, topic_id, alias, project_id, status, created_at) VALUES (?, ?, ?, ?, 'accepted', ?)",
                            (new_id("tal_"), identifier, alias, scope.project_id, now_iso()),
                        )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return identifier

    def topic_candidates(self, *, scope: Scope, label: str) -> list[dict[str, Any]]:
        """Every topic this label could mean, in this space, never silently one."""

        needle = label.strip().lower()
        with self._db() as db:
            rows = db.execute(
                """SELECT t.topic_id, t.canonical_label, a.alias, a.project_id, a.status
                   FROM topics t
                   LEFT JOIN topic_aliases a ON a.topic_id = t.topic_id
                   WHERE t.knowledge_space_id = ? AND t.status = 'active'
                     AND (lower(t.canonical_label) = ? OR lower(a.alias) = ?)
                   ORDER BY t.topic_id""",
                (scope.knowledge_space_id, needle, needle),
            ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = found.setdefault(
                row["topic_id"],
                {
                    "topic_id": row["topic_id"],
                    "canonical_label": row["canonical_label"],
                    "aliases": [],
                    "projects": [],
                },
            )
            if row["alias"]:
                entry["aliases"].append(row["alias"])
                if row["project_id"] not in entry["projects"]:
                    entry["projects"].append(row["project_id"])
        return list(found.values())

    def link_topic_claim(self, *, scope: Scope, topic_id: str, claim_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT OR IGNORE INTO topic_claims(knowledge_space_id, project_id, topic_id, claim_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (scope.knowledge_space_id, scope.project_id, topic_id, claim_id, now_iso()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def merge_topics(
        self,
        *,
        scope: Scope,
        source_topic_id: str,
        target_topic_id: str,
        actor_subject: str,
    ) -> dict[str, Any]:
        """Retire a topic by mapping it onto another, keeping the old id readable.

        The old id is not deleted and not reused. A reversal reads the mapping log
        and the affected-object list instead of guessing which rows moved.
        """

        if source_topic_id == target_topic_id:
            raise ClaimStoreError("A topic cannot be merged into itself.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                links = db.execute(
                    "SELECT project_id, claim_id FROM topic_claims WHERE topic_id = ?", (source_topic_id,)
                ).fetchall()
                for link in links:
                    db.execute(
                        "INSERT OR IGNORE INTO topic_claims(knowledge_space_id, project_id, topic_id, claim_id, created_at) VALUES (?, ?, ?, ?, ?)",
                        (scope.knowledge_space_id, link["project_id"], target_topic_id, link["claim_id"], now_iso()),
                    )
                db.execute(
                    "UPDATE topics SET status = 'merged', merged_into_topic_id = ? WHERE topic_id = ?",
                    (target_topic_id, source_topic_id),
                )
                db.execute(
                    "UPDATE topics SET status = 'merged', merged_into_topic_id = ? WHERE merged_into_topic_id = ?",
                    (target_topic_id, source_topic_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "source_topic_id": source_topic_id,
            "target_topic_id": target_topic_id,
            "moved_claims": len(links),
            "actor_subject": actor_subject,
        }

    def topic_merge_log(self, scope: Scope) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT topic_id, canonical_label, merged_into_topic_id FROM topics WHERE knowledge_space_id = ? AND status = 'merged'",
                (scope.knowledge_space_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    CHANGE_SET_FIELDS = frozenset(
        {
            "schema_version",
            "knowledge_space_id",
            "project_id",
            "run_id",
            "base_version",
            "claims",
            "relations",
            "topics",
            "reviews",
            "dropped",
            "dirty_pages",
        }
    )

    def _validate_changeset(self, changeset: Mapping[str, Any], scope: Scope) -> None:
        if not isinstance(changeset, Mapping):
            raise ClaimStoreError("A change set must be an object.")
        if changeset.get("schema_version") != 2:
            raise ClaimStoreError("Unsupported change set schema version.")
        # A field the store does not read is either a mistake or an attempt to
        # smuggle in a shape something else will act on. Both are refused, and
        # naming them makes the refusal actionable.
        unknown = sorted(set(changeset) - self.CHANGE_SET_FIELDS)
        if unknown:
            raise ClaimStoreError(
                "Change set carries fields this store does not accept: " + ", ".join(unknown)
            )
        if changeset.get("project_id") != scope.project_id:
            raise ScopeError(
                f"Change set targets project {changeset.get('project_id')}, not {scope.project_id}."
            )
        if changeset.get("knowledge_space_id") != scope.knowledge_space_id:
            raise ScopeError("Change set targets a different knowledge space.")
        for claim in changeset.get("claims", []):
            state = ClaimState.from_dict(claim["state"])
            validate_claim_state(state, claim.get("support", ()))
            for evidence_id in claim.get("evidence_refs", ()):
                if not str(evidence_id).startswith("evd_"):
                    raise ClaimStoreError(f"Malformed evidence reference: {evidence_id!r}.")

    def commit_changes(
        self,
        *,
        actor_subject: str,
        base_version: int,
        idempotency_key: str,
        changeset: Mapping[str, Any],
        run_id: str = "",
        project_id: str | None = None,
    ) -> CommitOutcome:
        """Write one knowledge change, or write nothing at all."""

        scope = self._scope(None, project_id)
        if not actor_subject or len(actor_subject) > ACTOR_LIMIT:
            raise ClaimStoreError("A verified actor subject is required.")
        if not isinstance(base_version, int) or base_version < 0:
            raise ClaimStoreError("base_version must be a non-negative integer.")
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
            raise ClaimStoreError("idempotency_key must be a short stable identifier.")
        self._validate_changeset(changeset, scope)

        run = run_id or str(changeset.get("run_id", ""))
        request_hash = hashlib.sha256(
            _json(
                {
                    "space": scope.knowledge_space_id,
                    "project": scope.project_id,
                    "base_version": base_version,
                    "changeset": changeset,
                }
            ).encode("utf-8")
        ).hexdigest()
        # The same intent, with the version the caller was on left out. A process
        # that dies between a successful commit and recording it comes back with a
        # refreshed base_version, and that retry is the same request rather than a
        # different one. Without this the run wedges forever on its own key.
        intent_hash = hashlib.sha256(
            _json(
                {
                    "space": scope.knowledge_space_id,
                    "project": scope.project_id,
                    # The change set carries its own base_version, so that field is
                    # removed here too. Leaving it in would make the retry look like
                    # a different request and put the wedge straight back.
                    "changeset": {key: value for key, value in changeset.items() if key != "base_version"},
                }
            ).encode("utf-8")
        ).hexdigest()

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT request_hash, intent_hash, result_json FROM knowledge_submissions WHERE knowledge_space_id = ? AND project_id = ? AND idempotency_key = ?",
                    (scope.knowledge_space_id, scope.project_id, idempotency_key),
                ).fetchone()
                if existing:
                    same_request = existing["request_hash"] == request_hash
                    same_intent = bool(existing["intent_hash"]) and existing["intent_hash"] == intent_hash
                    if not same_request and not same_intent:
                        db.rollback()
                        raise IdempotencyError(
                            "Idempotency key was already used for a different request."
                        )
                    db.commit()
                    stored = json.loads(existing["result_json"])
                    return CommitOutcome(**stored)

                current = self._ensure_meta(db, scope)
                if base_version != current:
                    db.rollback()
                    raise ConflictError(
                        f"Knowledge changed since base_version {base_version}; retry from version {current}.",
                        current_version=current,
                    )

                stamp = now_iso()
                # The write happens once. The next version number is stamped on
                # whatever it writes, and the counter only moves if it wrote
                # knowledge, so a commit that changed nothing leaves the version
                # where it was rather than reporting activity that did not happen.
                outcome = self._write_changes(
                    db=db,
                    scope=scope,
                    changeset=changeset,
                    actor_subject=actor_subject,
                    knowledge_version=current + 1,
                    stamp=stamp,
                    run_id=run,
                )
                if self._changed_anything(outcome):
                    db.execute(
                        "UPDATE knowledge_meta SET knowledge_version = ?, updated_at = ? WHERE project_id = ?",
                        (current + 1, stamp, scope.project_id),
                    )
                else:
                    # Report the version the project is actually at, not the one
                    # this commit would have used.
                    outcome = CommitOutcome(
                        **{**outcome.as_dict(), "status": "noop", "knowledge_version": current}
                    )
                db.execute(
                    "INSERT INTO knowledge_submissions(knowledge_space_id, project_id, idempotency_key, request_hash, intent_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        scope.knowledge_space_id,
                        scope.project_id,
                        idempotency_key,
                        request_hash,
                        intent_hash,
                        _json(outcome.as_dict()),
                        stamp,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return outcome

    @staticmethod
    def _changed_anything(outcome: CommitOutcome) -> bool:
        """Whether a commit moved the knowledge itself.

        A pending review and a recorded drop are bookkeeping about candidates, not
        knowledge a reader can rely on, so neither advances the knowledge version.
        A claim version or a relation does.
        """

        return bool(outcome.created_claims or outcome.updated_claims or outcome.new_relations)

    def _write_changes(
        self,
        *,
        db: sqlite3.Connection,
        scope: Scope,
        changeset: Mapping[str, Any],
        actor_subject: str,
        knowledge_version: int,
        stamp: str,
        run_id: str,
    ) -> CommitOutcome:
        created: list[str] = []
        updated: list[str] = []
        unchanged_count = 0
        warnings: list[str] = []

        for claim in changeset.get("claims", []):
            claim_id, created_now, is_unchanged = self._write_claim(
                db=db,
                scope=scope,
                claim=claim,
                actor_subject=actor_subject,
                knowledge_version=knowledge_version,
                stamp=stamp,
            )
            if is_unchanged:
                unchanged_count += 1
            elif created_now:
                created.append(claim_id)
            else:
                updated.append(claim_id)
            for topic_id in claim.get("topic_ids", ()):
                db.execute(
                    "INSERT OR IGNORE INTO topic_claims(knowledge_space_id, project_id, topic_id, claim_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (scope.knowledge_space_id, scope.project_id, topic_id, claim_id, stamp),
                )

        new_relations = 0
        for relation in changeset.get("relations", []):
            if self._write_relation(
                db=db, scope=scope, relation=relation, actor_subject=actor_subject, stamp=stamp
            ):
                new_relations += 1

        for topic in changeset.get("topics", []):
            label = str(topic.get("canonical_label", "")).strip()
            if not label:
                continue
            topic_id = str(topic.get("topic_id") or new_id("top_"))
            db.execute(
                "INSERT OR IGNORE INTO topics(topic_id, knowledge_space_id, canonical_label, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                (topic_id, scope.knowledge_space_id, label, stamp),
            )
            for alias in topic.get("aliases", ()):
                cleaned = str(alias).strip()
                if not cleaned:
                    continue
                exists = db.execute(
                    "SELECT alias_id FROM topic_aliases WHERE topic_id = ? AND alias = ? AND project_id = ?",
                    (topic_id, cleaned, scope.project_id),
                ).fetchone()
                if not exists:
                    db.execute(
                        "INSERT INTO topic_aliases(alias_id, topic_id, alias, project_id, status, created_at) VALUES (?, ?, ?, ?, 'accepted', ?)",
                        (new_id("tal_"), topic_id, cleaned, scope.project_id, stamp),
                    )

        reviews = changeset.get("reviews", [])
        for review in reviews:
            db.execute(
                """INSERT INTO review_queue(
                       review_id, knowledge_space_id, project_id, subject_kind, subject_id, subject_version,
                       question, trigger_code, candidates_json, evidence_refs_json, impact_json, status, run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (
                    str(review.get("review_id") or new_id("revq_")),
                    scope.knowledge_space_id,
                    scope.project_id,
                    str(review.get("subject_kind", "claim")),
                    str(review.get("subject_id", "")),
                    str(review.get("subject_version", "")),
                    str(review.get("question", "")),
                    str(review.get("trigger_code", "ambiguous_identity")),
                    _json(review.get("candidates", [])),
                    _json(review.get("evidence_refs", [])),
                    _json(review.get("impact", {})),
                    run_id,
                    stamp,
                ),
            )

        dropped = changeset.get("dropped", [])
        for item in dropped:
            # A drop is a decision about knowledge, so it is recorded here rather
            # than against a run. Writing it against a run that was never
            # registered made the whole commit fail its foreign key.
            db.execute(
                """INSERT OR REPLACE INTO dispositions(
                       disposition_id, knowledge_space_id, project_id, run_id, unit_id,
                       statement, disposition, reason_codes_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("dsp_"),
                    scope.knowledge_space_id,
                    scope.project_id,
                    run_id,
                    str(item.get("unit_id", item.get("statement", "")))[:120],
                    str(item.get("statement", ""))[:4_000],
                    str(item.get("disposition", "DROP")),
                    _json(list(item.get("reason_codes", ()))),
                    stamp,
                ),
            )

        for page_slug in changeset.get("dirty_pages", ()):
            db.execute(
                "UPDATE page_projections SET dirty = 1, projection_status = 'stale' WHERE project_id = ? AND slug = ?",
                (scope.project_id, page_slug),
            )

        return CommitOutcome(
            run_id=run_id,
            knowledge_version=knowledge_version,
            status="committed",
            created_claims=len(created),
            updated_claims=len(updated),
            new_relations=new_relations,
            review_pending=len(reviews),
            dropped_candidates=len(dropped),
            projection_status="pending" if changeset.get("dirty_pages") else "not_requested",
            created_claim_ids=tuple(created),
            updated_claim_ids=tuple(updated),
            unchanged_claims=unchanged_count,
            warnings=tuple(warnings),
        )

    def _write_claim(
        self,
        *,
        db: sqlite3.Connection,
        scope: Scope,
        claim: Mapping[str, Any],
        actor_subject: str,
        knowledge_version: int,
        stamp: str,
    ) -> tuple[str, bool, bool]:
        """Write one claim. Returns (claim_id, created, unchanged).

        A second import of the same material must not mint a version. It earns one
        only when the wording, the qualifiers, the status axes, the attribution or
        the evidence set actually changed.

        When the proposition is unchanged and only the evidence grew, the previous
        origins carry forward. Two independent sources then stay two independent
        support groups on one version, which is what lets one of them be withdrawn
        without the claim losing its support. A changed statement carries nothing
        forward, because old grounding must never be reused for new wording.
        """

        state = ClaimState.from_dict(claim["state"])
        statement = str(claim["statement"]).strip()
        if not statement:
            raise ClaimStoreError("A claim needs a statement.")
        conditions = tuple(str(item) for item in claim.get("conditions", ()))
        fingerprint = statement_fingerprint(scope, statement, conditions)

        claim_id, existing_claim = self._resolve_claim_id(db, scope, claim, fingerprint)
        current_version_id = existing_claim["current_version_id"] if existing_claim else None

        if current_version_id and self._claim_unchanged(
            db=db,
            claim_version_id=current_version_id,
            claim=claim,
            state=state,
            statement=statement,
            conditions=conditions,
        ):
            return claim_id, False, True

        if current_version_id:
            latest = db.execute(
                "SELECT MAX(version) AS version FROM claim_versions WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            version_number = int(latest["version"]) + 1
            created_now = False
        else:
            version_number = 1
            created_now = True
            db.execute(
                "INSERT INTO claims(claim_id, knowledge_space_id, project_id, fingerprint, current_version_id, lifecycle_status, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    claim_id,
                    scope.knowledge_space_id,
                    scope.project_id,
                    fingerprint,
                    state.lifecycle_status,
                    stamp,
                    stamp,
                ),
            )

        # The store mints this. A model that echoed an id back gets a fresh one,
        # which is what keeps a fabricated version from becoming authoritative.
        claim_version_id = new_id("clv_")
        attribution = claim.get("attribution", {}) or {}
        db.execute(
            """INSERT INTO claim_versions(
                   claim_version_id, claim_id, knowledge_space_id, project_id, version, statement,
                   knowledge_kind, derivation, epistemic_status, lifecycle_status, grounding_status,
                   decision_state, question_state, conditions_json, asserted_by, asserted_at,
                   asserted_at_precision, valid_from, valid_to, attributes_json, created_at,
                   committed_at, actor_subject, knowledge_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_version_id,
                claim_id,
                scope.knowledge_space_id,
                scope.project_id,
                version_number,
                statement,
                state.knowledge_kind,
                state.derivation,
                state.epistemic_status,
                state.lifecycle_status,
                state.grounding_status,
                state.decision_state,
                state.question_state,
                _json(list(conditions)),
                str(attribution.get("asserted_by", "unknown")),
                attribution.get("asserted_at"),
                str(attribution.get("asserted_at_precision", "unknown")),
                claim.get("valid_from"),
                claim.get("valid_to"),
                _json(claim.get("attributes", {})),
                stamp,
                stamp,
                actor_subject,
                knowledge_version,
            ),
        )
        for position, subject in enumerate(claim.get("subjects", ())):
            db.execute(
                "INSERT OR IGNORE INTO claim_subjects(claim_version_id, subject, position) VALUES (?, ?, ?)",
                (claim_version_id, str(subject), position),
            )

        for origin in self._carried_origins(
            db=db, parent_version_id=current_version_id, statement=statement, conditions=conditions
        ):
            self._write_origin(
                db=db,
                scope=scope,
                claim_version_id=claim_version_id,
                origin=origin,
                default_asserted_by=str(attribution.get("asserted_by", "unknown")),
                stamp=stamp,
            )

        for origin in claim.get("origins", ()):
            self._write_origin(
                db=db,
                scope=scope,
                claim_version_id=claim_version_id,
                origin=origin,
                default_asserted_by=str(attribution.get("asserted_by", "unknown")),
                stamp=stamp,
            )

        db.execute(
            "UPDATE claims SET current_version_id = ?, updated_at = ?, lifecycle_status = ? WHERE claim_id = ?",
            (claim_version_id, stamp, state.lifecycle_status, claim_id),
        )
        return claim_id, created_now, False

    def _resolve_claim_id(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        claim: Mapping[str, Any],
        fingerprint: str,
    ) -> tuple[str, sqlite3.Row | None]:
        """Find the claim this proposition belongs to, or mint a new identity."""

        requested_id = claim.get("claim_id")
        if requested_id is None:
            existing = db.execute(
                "SELECT claim_id, current_version_id FROM claims WHERE project_id = ? AND fingerprint = ? AND lifecycle_status = 'active'",
                (scope.project_id, fingerprint),
            ).fetchone()
            if existing:
                return existing["claim_id"], existing
            return new_id("clm_"), None
        if not CLAIM_ID_PATTERN.fullmatch(str(requested_id)):
            raise ClaimStoreError(f"Malformed claim id: {requested_id!r}.")
        # An id that already belongs to this project is honoured only when the
        # caller is updating it. An id the store never issued is not created.
        row = db.execute(
            "SELECT claim_id, current_version_id FROM claims WHERE claim_id = ? AND project_id = ?",
            (requested_id, scope.project_id),
        ).fetchone()
        if not row:
            raise ScopeError(f"Claim {requested_id} does not exist in project {scope.project_id}.")
        return str(requested_id), row

    @staticmethod
    def _carried_origins(
        *,
        db: sqlite3.Connection,
        parent_version_id: str | None,
        statement: str,
        conditions: Sequence[str],
    ) -> list[dict[str, Any]]:
        """The prior origins of a proposition whose meaning has not changed."""

        if not parent_version_id:
            return []
        parent = db.execute(
            "SELECT statement, conditions_json FROM claim_versions WHERE claim_version_id = ?",
            (parent_version_id,),
        ).fetchone()
        if not parent:
            return []
        if parent["statement"] != statement or json.loads(parent["conditions_json"]) != list(conditions):
            return []
        rows = db.execute(
            "SELECT * FROM claim_origins WHERE claim_version_id = ? ORDER BY created_at, origin_id",
            (parent_version_id,),
        ).fetchall()
        carried: list[dict[str, Any]] = []
        for row in rows:
            evidence = db.execute(
                "SELECT evidence_id FROM claim_evidence WHERE origin_id = ? ORDER BY position",
                (row["origin_id"],),
            ).fetchall()
            premises = db.execute(
                """SELECT requirement_id FROM claim_support_requirements
                   WHERE claim_version_id = ? AND support_group_id = ? AND requirement_kind = 'claim_version'
                   ORDER BY position""",
                (parent_version_id, row["support_group_id"]),
            ).fetchall()
            carried.append(
                {
                    "derivation": row["derivation"],
                    "inference_note": row["inference_note"],
                    "assumptions": json.loads(row["assumptions_json"]),
                    "support_group_id": row["support_group_id"],
                    "origin_status": row["origin_status"],
                    "asserted_by": row["asserted_by"],
                    "asserted_at": row["asserted_at"],
                    "evidence_refs": [item["evidence_id"] for item in evidence],
                    "premise_claim_version_ids": [item["requirement_id"] for item in premises],
                }
            )
        return carried

    def _write_origin(
        self,
        *,
        db: sqlite3.Connection,
        scope: Scope,
        claim_version_id: str,
        origin: Mapping[str, Any],
        default_asserted_by: str,
        stamp: str,
    ) -> str:
        """Write one origin with its evidence links and support requirements."""

        origin_id = new_id("org_")
        support_group_id = str(origin.get("support_group_id") or new_id("sgr_"))
        db.execute(
            """INSERT INTO claim_origins(
                   origin_id, claim_version_id, derivation, inference_note, assumptions_json,
                   support_group_id, origin_status, asserted_by, asserted_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                origin_id,
                claim_version_id,
                str(origin.get("derivation", "explicit")),
                str(origin.get("inference_note", "")),
                _json(list(origin.get("assumptions", ()))),
                support_group_id,
                str(origin.get("origin_status", "active")),
                str(origin.get("asserted_by", default_asserted_by)),
                origin.get("asserted_at"),
                stamp,
            ),
        )
        seen_evidence: list[str] = []
        for evidence_id in origin.get("evidence_refs", ()):
            if evidence_id in seen_evidence:
                # One support group cites one address once. Repeating it is a slip
                # rather than a stronger requirement, so it is folded here instead
                # of colliding with the primary key.
                continue
            seen_evidence.append(evidence_id)
            position = len(seen_evidence) - 1
            owner = db.execute(
                "SELECT project_id FROM evidence_refs WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            if not owner:
                raise ClaimStoreError(f"Unknown evidence reference: {evidence_id}.")
            # The composite foreign key would catch this too. Checking first turns a
            # constraint violation into a named scope error a caller can act on.
            if owner["project_id"] != scope.project_id:
                raise ScopeError(
                    f"Evidence {evidence_id} belongs to project {owner['project_id']}, not {scope.project_id}."
                )
            db.execute(
                "INSERT INTO claim_evidence(origin_id, knowledge_space_id, project_id, evidence_id, position) VALUES (?, ?, ?, ?, ?)",
                (origin_id, scope.knowledge_space_id, scope.project_id, evidence_id, position),
            )
            db.execute(
                """INSERT OR REPLACE INTO claim_support_requirements(
                       support_group_id, claim_version_id, requirement_kind, requirement_id, position, available)
                   VALUES (?, ?, 'evidence', ?, ?, ?)""",
                (
                    support_group_id,
                    claim_version_id,
                    evidence_id,
                    position,
                    _as_bool(self._evidence_available(db, evidence_id)),
                ),
            )
        for position, premise in enumerate(origin.get("premise_claim_version_ids", ())):
            if not str(premise).startswith("clv_"):
                raise ClaimStoreError(f"Malformed premise id: {premise!r}.")
            row = db.execute(
                "SELECT project_id FROM claim_versions WHERE claim_version_id = ?", (premise,)
            ).fetchone()
            if not row:
                raise ClaimStoreError(f"Unknown premise claim version: {premise}.")
            if row["project_id"] != scope.project_id:
                raise ScopeError(
                    f"Premise {premise} belongs to project {row['project_id']}, not {scope.project_id}."
                )
            db.execute(
                """INSERT OR REPLACE INTO claim_support_requirements(
                       support_group_id, claim_version_id, requirement_kind, requirement_id, position, available)
                   VALUES (?, ?, 'claim_version', ?, ?, 1)""",
                (support_group_id, claim_version_id, premise, position),
            )

        premise_ids = list(origin.get("premise_claim_version_ids", ()))
        if premise_ids:
            edges = db.execute(
                "SELECT from_claim_version_id, to_claim_version_id FROM claim_relations WHERE relation_type = 'derived_from'"
            ).fetchall()
            prospective = [(row["from_claim_version_id"], row["to_claim_version_id"]) for row in edges]
            prospective.extend((claim_version_id, premise) for premise in premise_ids)
            cycle = find_derivation_cycle([claim_version_id, *premise_ids], prospective)
            if cycle:
                raise ClaimStoreError(
                    "A derivation cycle would be created: " + " -> ".join(cycle) + "."
                )
        return origin_id

    @staticmethod
    def _claim_unchanged(
        *,
        db: sqlite3.Connection,
        claim_version_id: str,
        claim: Mapping[str, Any],
        state: ClaimState,
        statement: str,
        conditions: Sequence[str],
    ) -> bool:
        """True when re-importing this claim would add nothing.

        A second import of the same material must not mint a version. It only
        earns one when the wording, the qualifiers, the status axes, the
        attribution or the evidence set actually changed.
        """

        current = db.execute(
            "SELECT * FROM claim_versions WHERE claim_version_id = ?", (claim_version_id,)
        ).fetchone()
        if not current:
            return False
        same_state = (
            current["statement"] == statement
            and current["knowledge_kind"] == state.knowledge_kind
            and current["derivation"] == state.derivation
            and current["epistemic_status"] == state.epistemic_status
            and current["lifecycle_status"] == state.lifecycle_status
            and current["decision_state"] == state.decision_state
            and current["question_state"] == state.question_state
            and json.loads(current["conditions_json"]) == list(conditions)
        )
        if not same_state:
            return False
        attribution = claim.get("attribution", {}) or {}
        same_attribution = (
            current["asserted_by"] == str(attribution.get("asserted_by", "unknown"))
            and current["asserted_at"] == attribution.get("asserted_at")
        )
        if not same_attribution:
            return False
        wanted = {
            str(evidence_id)
            for origin in claim.get("origins", ())
            for evidence_id in origin.get("evidence_refs", ())
        }
        present = {
            row["evidence_id"]
            for row in db.execute(
                """SELECT e.evidence_id FROM claim_evidence e
                   JOIN claim_origins o ON o.origin_id = e.origin_id
                   WHERE o.claim_version_id = ?""",
                (claim_version_id,),
            )
        }
        return bool(wanted) and wanted <= present

    @staticmethod
    def _evidence_available(db: sqlite3.Connection, evidence_id: str) -> bool:
        row = db.execute("SELECT withdrawn_at FROM evidence_refs WHERE evidence_id = ?", (evidence_id,)).fetchone()
        return bool(row) and row["withdrawn_at"] is None

    def _write_relation(
        self,
        *,
        db: sqlite3.Connection,
        scope: Scope,
        relation: Mapping[str, Any],
        actor_subject: str,
        stamp: str,
    ) -> bool:
        relation_type = str(relation["relation_type"])
        status = str(relation.get("relation_status", "proposed"))
        if status not in RELATION_STATUSES:
            raise ClaimStoreError(f"Unknown relation status: {status!r}.")
        endpoints = []
        for key in ("from_claim_version_id", "to_claim_version_id"):
            value = str(relation[key])
            row = db.execute(
                """SELECT v.project_id, v.knowledge_kind FROM claim_versions v
                   WHERE v.claim_version_id = ?""",
                (value,),
            ).fetchone()
            if not row:
                raise ClaimStoreError(f"Unknown relation endpoint: {value}.")
            if row["project_id"] != scope.project_id:
                raise ScopeError(
                    f"Relation endpoint {value} belongs to project {row['project_id']}, not {scope.project_id}."
                )
            endpoints.append((value, row["knowledge_kind"]))
        (from_id, from_kind), (to_id, to_kind) = endpoints
        if from_id == to_id:
            raise ClaimStoreError("A claim cannot relate to itself.")
        validate_relation_endpoint_kinds(relation_type, from_kind, to_kind)
        if relation_type == "derived_from":
            edges = db.execute(
                "SELECT from_claim_version_id, to_claim_version_id FROM claim_relations WHERE relation_type = 'derived_from'"
            ).fetchall()
            prospective = [(row["from_claim_version_id"], row["to_claim_version_id"]) for row in edges]
            prospective.append((from_id, to_id))
            cycle = find_derivation_cycle([from_id, to_id], prospective)
            if cycle:
                raise ClaimStoreError("A derivation cycle would be created: " + " -> ".join(cycle) + ".")
        claim_ids = {}
        for value in (from_id, to_id):
            row = db.execute("SELECT claim_id FROM claim_versions WHERE claim_version_id = ?", (value,)).fetchone()
            claim_ids[value] = row["claim_id"]
        sql = """INSERT OR IGNORE INTO claim_relations(
                     relation_id, knowledge_space_id, project_id, relation_type, relation_status,
                     from_claim_version_id, to_claim_version_id, from_claim_id, to_claim_id,
                     origin_evidence_json, recorded_by, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        parameters = (
            str(relation.get("relation_id") or new_id("rel_")),
            scope.knowledge_space_id,
            scope.project_id,
            relation_type,
            status,
            from_id,
            to_id,
            claim_ids[from_id],
            claim_ids[to_id],
            _json(list(relation.get("origin_evidence_refs", ()))),
            actor_subject,
            stamp,
        )
        if relation_type == "contradicts" and from_id > to_id:
            parameters = (parameters[0], parameters[1], parameters[2], parameters[3], parameters[4], to_id, from_id, claim_ids[to_id], claim_ids[from_id], *parameters[9:])
        cursor = db.execute(sql, parameters)
        return cursor.rowcount > 0

    def _recompute_support(self, db: sqlite3.Connection, scope: Scope) -> int:
        """Re-derive grounding from the support graph, to a fixpoint.

        A whole group keeps the claim grounded. A partly available group marks it
        for revalidation. Nothing available makes it unsupported. Availability is
        read from the evidence and premise rows themselves rather than from a
        cached flag, so withdrawing a source takes effect on the next pass.

        The loop exists because a premise that lost its own support also stops
        being a usable premise. It runs to a fixpoint bounded by the number of
        claim versions, which is enough for a chain and refuses to spin.
        """

        scope_rows = db.execute(
            """SELECT DISTINCT v.claim_version_id
               FROM claim_versions v
               JOIN claim_support_requirements r ON r.claim_version_id = v.claim_version_id
               WHERE v.project_id = ?""",
            (scope.project_id,),
        ).fetchall()
        if not scope_rows:
            return 0
        limit = len(scope_rows) + 1
        touched = 0
        for _ in range(limit):
            requirements = db.execute(
                """SELECT r.claim_version_id, r.support_group_id, r.requirement_kind,
                          r.requirement_id, r.available,
                          e.withdrawn_at AS evidence_withdrawn,
                          pv.lifecycle_status AS premise_lifecycle,
                          pv.grounding_status AS premise_grounding
                   FROM claim_support_requirements r
                   JOIN claim_versions v ON v.claim_version_id = r.claim_version_id
                   LEFT JOIN evidence_refs e
                       ON r.requirement_kind = 'evidence' AND e.evidence_id = r.requirement_id
                   LEFT JOIN claim_versions pv
                       ON r.requirement_kind = 'claim_version' AND pv.claim_version_id = r.requirement_id
                   WHERE v.project_id = ?""",
                (scope.project_id,),
            ).fetchall()
            by_version: dict[str, dict[str, list[bool]]] = {}
            availability: dict[tuple[str, str, str], bool] = {}
            for row in requirements:
                if row["requirement_kind"] == "evidence":
                    live = row["evidence_withdrawn"] is None and row["requirement_id"] is not None
                else:
                    live = (
                        row["premise_lifecycle"] is not None
                        and row["premise_lifecycle"] != "retracted"
                        and row["premise_grounding"] != "unsupported"
                    )
                availability[
                    (row["claim_version_id"], row["requirement_kind"], row["requirement_id"])
                ] = live
                by_version.setdefault(row["claim_version_id"], {}).setdefault(
                    row["support_group_id"], []
                ).append(live)

            changed = 0
            for claim_version_id, by_group in by_version.items():
                outcome = support_group_outcome(list(by_group.values()))
                cursor = db.execute(
                    "UPDATE claim_versions SET grounding_status = ? WHERE claim_version_id = ? AND grounding_status != ?",
                    (outcome, claim_version_id, outcome),
                )
                changed += cursor.rowcount
            for (claim_version_id, kind, requirement_id), live in availability.items():
                cursor = db.execute(
                    """UPDATE claim_support_requirements SET available = ?
                       WHERE claim_version_id = ? AND requirement_kind = ? AND requirement_id = ? AND available != ?""",
                    (_as_bool(live), claim_version_id, kind, requirement_id, _as_bool(live)),
                )
                changed += cursor.rowcount
            touched += changed
            if not changed:
                break
        return touched

    def refresh_support(self, *, scope: Scope, evidence_id: str | None = None, claim_version_id: str | None = None) -> int:
        """Recompute grounding after evidence or a premise changed."""

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if evidence_id is not None:
                    db.execute(
                        "UPDATE claim_support_requirements SET available = 0 WHERE requirement_kind = 'evidence' AND requirement_id = ? AND ? IS NOT NULL",
                        (evidence_id, evidence_id),
                    )
                if claim_version_id is not None:
                    db.execute(
                        "UPDATE claim_support_requirements SET available = 0 WHERE requirement_kind = 'claim_version' AND requirement_id = ?",
                        (claim_version_id,),
                    )
                touched = self._recompute_support(db, scope)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return touched

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------

    @staticmethod
    def _claim_version_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "claim_version_id": row["claim_version_id"],
            "claim_id": row["claim_id"],
            "knowledge_space_id": row["knowledge_space_id"],
            "project_id": row["project_id"],
            "version": row["version"],
            "statement": row["statement"],
            "knowledge_kind": row["knowledge_kind"],
            "derivation": row["derivation"],
            "epistemic_status": row["epistemic_status"],
            "lifecycle_status": row["lifecycle_status"],
            "grounding_status": row["grounding_status"],
            "decision_state": row["decision_state"],
            "question_state": row["question_state"],
            "conditions": json.loads(row["conditions_json"]),
            "asserted_by": row["asserted_by"],
            "asserted_at": row["asserted_at"],
            "asserted_at_precision": row["asserted_at_precision"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "attributes": json.loads(row["attributes_json"]),
            "committed_at": row["committed_at"],
            "actor_subject": row["actor_subject"],
            "knowledge_version": row["knowledge_version"],
        }

    def get_claim(
        self,
        claim_id: str,
        scope: Scope,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        """One claim with every origin, every support group and every relation."""

        with self._db() as db:
            claim = db.execute(
                "SELECT * FROM claims WHERE claim_id = ? AND project_id = ?",
                (claim_id, scope.project_id),
            ).fetchone()
            if not claim:
                elsewhere = db.execute(
                    "SELECT project_id FROM claims WHERE claim_id = ?", (claim_id,)
                ).fetchone()
                if elsewhere:
                    raise ScopeError(
                        f"Claim {claim_id} belongs to project {elsewhere['project_id']}, not {scope.project_id}."
                    )
                raise ClaimStoreError(f"Unknown claim: {claim_id}.")
            if version is None:
                rows = db.execute(
                    "SELECT * FROM claim_versions WHERE claim_id = ? ORDER BY version DESC LIMIT 1",
                    (claim_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM claim_versions WHERE claim_id = ? AND version = ?",
                    (claim_id, version),
                ).fetchall()
            if not rows:
                raise ClaimStoreError(f"Claim {claim_id} has no version {version}.")
            current = self._claim_version_row(rows[0])
            origins = self._origins_for(db, current["claim_version_id"])
            history = db.execute(
                "SELECT version, claim_version_id, statement, epistemic_status, decision_state, lifecycle_status, committed_at FROM claim_versions WHERE claim_id = ? ORDER BY version",
                (claim_id,),
            ).fetchall()
            relations = db.execute(
                "SELECT * FROM claim_relations WHERE from_claim_id = ? OR to_claim_id = ? ORDER BY created_at, relation_id",
                (claim_id, claim_id),
            ).fetchall()
            topics = db.execute(
                "SELECT topic_id FROM topic_claims WHERE claim_id = ? AND project_id = ?",
                (claim_id, scope.project_id),
            ).fetchall()
        return {
            "claim_id": claim_id,
            "knowledge_space_id": claim["knowledge_space_id"],
            "project_id": claim["project_id"],
            "fingerprint": claim["fingerprint"],
            "current_version_id": claim["current_version_id"],
            "selected_version": current,
            "origins": origins,
            "history": [dict(row) for row in history],
            "relations": [dict(row) for row in relations],
            "topic_ids": [row["topic_id"] for row in topics],
        }

    def _origins_for(self, db: sqlite3.Connection, claim_version_id: str) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT * FROM claim_origins WHERE claim_version_id = ? ORDER BY created_at, origin_id",
            (claim_version_id,),
        ).fetchall()
        origins: list[dict[str, Any]] = []
        for row in rows:
            evidence = db.execute(
                "SELECT evidence_id FROM claim_evidence WHERE origin_id = ? ORDER BY position",
                (row["origin_id"],),
            ).fetchall()
            requirements = db.execute(
                """SELECT requirement_kind, requirement_id, available FROM claim_support_requirements
                   WHERE support_group_id = ? AND claim_version_id = ? ORDER BY requirement_kind, position""",
                (row["support_group_id"], claim_version_id),
            ).fetchall()
            origins.append(
                {
                    "origin_id": row["origin_id"],
                    "derivation": row["derivation"],
                    "inference_note": row["inference_note"],
                    "assumptions": json.loads(row["assumptions_json"]),
                    "support_group_id": row["support_group_id"],
                    "origin_status": row["origin_status"],
                    "asserted_by": row["asserted_by"],
                    "asserted_at": row["asserted_at"],
                    "evidence_refs": [item["evidence_id"] for item in evidence],
                    "support_requirements": [
                        {
                            "kind": item["requirement_kind"],
                            "id": item["requirement_id"],
                            "available": bool(item["available"]),
                        }
                        for item in requirements
                    ],
                }
            )
        return origins

    def claim_versions_for_evidence(self, evidence_id: str, scope: Scope) -> list[str]:
        with self._db() as db:
            rows = db.execute(
                """SELECT DISTINCT o.claim_version_id
                   FROM claim_evidence e
                   JOIN claim_origins o ON o.origin_id = e.origin_id
                   WHERE e.evidence_id = ? AND e.project_id = ?""",
                (evidence_id, scope.project_id),
            ).fetchall()
        return [row["claim_version_id"] for row in rows]

    def iter_claims(
        self,
        scope: Scope,
        *,
        include_history: bool = False,
        lifecycle: Sequence[str] | None = None,
        knowledge_kinds: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Claims visible to one project, always with the project on each row.

        History is opt-in. A default read answers "what do we currently hold",
        and the historical view is a separate request so the two cannot be
        confused by a caller that forgot which one it wanted.
        """

        query = [
            "SELECT v.*, c.fingerprint, c.current_version_id FROM claim_versions v",
            "JOIN claims c ON c.claim_id = v.claim_id",
            "WHERE v.project_id = ?",
        ]
        parameters: list[Any] = [scope.project_id]
        if not include_history:
            query.append("AND v.claim_version_id = c.current_version_id")
        if lifecycle:
            query.append(f"AND v.lifecycle_status IN ({','.join('?' * len(lifecycle))})")
            parameters.extend(lifecycle)
        if knowledge_kinds:
            query.append(f"AND v.knowledge_kind IN ({','.join('?' * len(knowledge_kinds))})")
            parameters.extend(knowledge_kinds)
        query.append("ORDER BY v.claim_id, v.version")
        with self._db() as db:
            rows = db.execute(" ".join(query), tuple(parameters)).fetchall()
        return [self._claim_version_row(row) for row in rows]

    def claim_ids_for_topic(self, topic_id: str, scope: Scope) -> list[str]:
        with self._db() as db:
            rows = db.execute(
                "SELECT claim_id FROM topic_claims WHERE topic_id = ? AND project_id = ?",
                (topic_id, scope.project_id),
            ).fetchall()
        return [row["claim_id"] for row in rows]

    def relations_of_type(self, relation_type: str, scope: Scope) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM claim_relations WHERE relation_type = ? AND project_id = ? ORDER BY created_at, relation_id",
                (relation_type, scope.project_id),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------

    def upsert_projection(
        self,
        *,
        scope: Scope,
        slug: str,
        title: str,
        markdown: str,
        manifest: Mapping[str, Any],
        content_sha256: str,
        renderer_version: str = "projection/1",
        page_kind: str = "topic",
        manual_edit_hash: str | None = None,
    ) -> dict[str, Any]:
        """Write a page and the claim versions it was built from, in one transaction.

        The manifest is the page's provenance. A page whose manifest does not name
        the claim versions it contains cannot be audited, so the two are written
        together and the page is only marked clean once both are in.
        """

        entries = list(manifest.get("entries", []))
        status = "manual" if manual_edit_hash else "current"
        manifest_payload = dict(manifest)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT page_id FROM page_projections WHERE project_id = ? AND slug = ?",
                    (scope.project_id, slug),
                ).fetchone()
                page_id = row["page_id"] if row else new_id("pag_")
                stamp = now_iso()
                db.execute(
                    """INSERT INTO page_projections(
                           page_id, knowledge_space_id, project_id, slug, title, page_kind,
                           renderer_version, manifest_json, markdown, content_sha256,
                           projection_status, dirty, manual_edit_hash, rendered_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                       ON CONFLICT(knowledge_space_id, project_id, slug) DO UPDATE SET
                           title = excluded.title,
                           page_kind = excluded.page_kind,
                           renderer_version = excluded.renderer_version,
                           manifest_json = excluded.manifest_json,
                           markdown = excluded.markdown,
                           content_sha256 = excluded.content_sha256,
                           projection_status = excluded.projection_status,
                           dirty = 0,
                           manual_edit_hash = excluded.manual_edit_hash,
                           rendered_at = excluded.rendered_at""",
                    (
                        page_id,
                        scope.knowledge_space_id,
                        scope.project_id,
                        slug,
                        title,
                        page_kind,
                        renderer_version,
                        _json(manifest_payload),
                        markdown,
                        content_sha256,
                        status,
                        manual_edit_hash,
                        stamp,
                    ),
                )
                stored = db.execute(
                    "SELECT page_id FROM page_projections WHERE project_id = ? AND slug = ?",
                    (scope.project_id, slug),
                ).fetchone()
                page_id = stored["page_id"]
                for topic_id in manifest_payload.get("topic_ids", ()):
                    db.execute(
                        "INSERT OR IGNORE INTO page_topics(page_id, topic_id) VALUES (?, ?)",
                        (page_id, topic_id),
                    )
                db.execute("DELETE FROM page_claims WHERE page_id = ?", (page_id,))
                for position, entry in enumerate(entries):
                    db.execute(
                        "INSERT OR REPLACE INTO page_claims(page_id, claim_version_id, section, position) VALUES (?, ?, ?, ?)",
                        (page_id, entry["claim_version_id"], entry["section"], position),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "page_id": page_id,
            "slug": slug,
            "entries": len(entries),
            "projection_status": status,
        }

    def projection(self, slug: str, scope: Scope) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM page_projections WHERE project_id = ? AND slug = ?",
                (scope.project_id, slug),
            ).fetchone()
            if not row:
                return None
            claims = db.execute(
                "SELECT claim_version_id, section, position FROM page_claims WHERE page_id = ? ORDER BY section, position",
                (row["page_id"],),
            ).fetchall()
        return {
            "page_id": row["page_id"],
            "slug": row["slug"],
            "title": row["title"],
            "markdown": row["markdown"],
            "manifest": json.loads(row["manifest_json"]),
            "content_sha256": row["content_sha256"],
            "renderer_version": row["renderer_version"],
            "projection_status": row["projection_status"],
            "dirty": bool(row["dirty"]),
            "manual_edit_hash": row["manual_edit_hash"],
            "rendered_at": row["rendered_at"],
            "claims": [dict(item) for item in claims],
        }

    def projections(self, scope: Scope, *, dirty_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM page_projections WHERE project_id = ?"
        if dirty_only:
            query += " AND dirty = 1"
        query += " ORDER BY slug"
        with self._db() as db:
            rows = db.execute(query, (scope.project_id,)).fetchall()
        return [
            {
                "page_id": row["page_id"],
                "slug": row["slug"],
                "title": row["title"],
                "projection_status": row["projection_status"],
                "dirty": bool(row["dirty"]),
                "content_sha256": row["content_sha256"],
                "manual_edit_hash": row["manual_edit_hash"],
            }
            for row in rows
        ]

    def mark_projection_dirty(self, slugs: Iterable[str], scope: Scope) -> int:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                touched = 0
                for slug in slugs:
                    cursor = db.execute(
                        "UPDATE page_projections SET dirty = 1, projection_status = 'stale' WHERE project_id = ? AND slug = ?",
                        (scope.project_id, slug),
                    )
                    touched += cursor.rowcount
                db.commit()
            except Exception:
                db.rollback()
                raise
        return touched

    def record_page_move(
        self,
        *,
        scope: Scope,
        old_slug: str,
        page_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Leave an address behind when a page moves, so an old link still resolves."""

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT OR REPLACE INTO page_redirects(knowledge_space_id, project_id, old_slug, page_id, reason, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (scope.knowledge_space_id, scope.project_id, old_slug, page_id, reason, now_iso()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"old_slug": old_slug, "page_id": page_id, "reason": reason, "status": "redirected"}

    def remove_projection(self, slug: str, scope: Scope) -> int:
        """Retire one page. Its claims stay in the knowledge layer untouched."""

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    "DELETE FROM page_projections WHERE project_id = ? AND slug = ?",
                    (scope.project_id, slug),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return cursor.rowcount

    def resolve_page_redirect(self, slug: str, scope: Scope) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                """SELECT r.old_slug, r.reason, r.page_id, p.slug AS current_slug, p.title
                   FROM page_redirects r
                   JOIN page_projections p ON p.page_id = r.page_id
                   WHERE r.project_id = ? AND r.old_slug = ?""",
                (scope.project_id, slug),
            ).fetchone()
        if not row:
            return None
        return {
            "old_slug": row["old_slug"],
            "current_slug": row["current_slug"],
            "title": row["title"],
            "reason": row["reason"],
            "page_id": row["page_id"],
            "status": "redirected",
        }

    def rebuild_queue(self, scope: Scope) -> dict[str, Any]:
        """Which pages need re-rendering, and which claims moved under them.

        A page is queued when a claim version it was built from is no longer that
        claim's current version, so an unrelated commit does not rebuild the whole
        wiki.
        """

        with self._db() as db:
            rows = db.execute(
                """SELECT p.slug, p.dirty, pc.claim_version_id, v.claim_id, c.current_version_id
                   FROM page_projections p
                   JOIN page_claims pc ON pc.page_id = p.page_id
                   JOIN claim_versions v ON v.claim_version_id = pc.claim_version_id
                   JOIN claims c ON c.claim_id = v.claim_id
                   WHERE p.project_id = ?""",
                (scope.project_id,),
            ).fetchall()
            known = [
                row["slug"]
                for row in db.execute(
                    "SELECT slug FROM page_projections WHERE project_id = ? ORDER BY slug",
                    (scope.project_id,),
                )
            ]
        stale: dict[str, list[str]] = {}
        for row in rows:
            if row["dirty"] or row["claim_version_id"] != row["current_version_id"]:
                stale.setdefault(row["slug"], []).append(row["claim_id"])
        return {
            "rebuild": sorted(stale),
            "reasons": {slug: sorted(set(claims)) for slug, claims in stale.items()},
            "unchanged": sorted(slug for slug in known if slug not in stale),
        }

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    def open_reviews(self, scope: Scope) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM review_queue WHERE project_id = ? AND status = 'open' ORDER BY created_at, review_id",
                (scope.project_id,),
            ).fetchall()
        return [
            {
                "review_id": row["review_id"],
                "subject_kind": row["subject_kind"],
                "subject_id": row["subject_id"],
                "subject_version": row["subject_version"],
                "question": row["question"],
                "trigger_code": row["trigger_code"],
                "candidates": json.loads(row["candidates_json"]),
                "evidence_refs": json.loads(row["evidence_refs_json"]),
                "impact": json.loads(row["impact_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def review_action(
        self,
        *,
        actor_subject: str,
        review_id: str,
        expected_version: str,
        action: str,
        scope: Scope,
        idempotency_key: str,
        note: str = "",
        edited_statement: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply one review decision, refusing to apply it to a moved object.

        `retain` records that the claim should stay in the knowledge base. It does
        not verify a hypothesis, adopt a proposal, or turn an inference into a
        quote. Those are separate actions with separate evidence requirements.
        """

        if action not in REVIEW_ACTIONS:
            raise ClaimStoreError(f"Unknown review action: {action!r}.")
        if not IDEMPOTENCY_PATTERN.fullmatch(str(idempotency_key or "")):
            raise ClaimStoreError("idempotency_key must be a short stable identifier.")
        if not actor_subject or len(actor_subject) > ACTOR_LIMIT:
            raise ClaimStoreError("A verified actor subject is required.")
        request_hash = hashlib.sha256(
            _json(
                {
                    "review_id": review_id,
                    "action": action,
                    "expected_version": expected_version,
                    "note": note,
                    "edited_statement": edited_statement,
                    "topic_id": topic_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                review = db.execute(
                    "SELECT * FROM review_queue WHERE review_id = ? AND project_id = ?",
                    (review_id, scope.project_id),
                ).fetchone()
                if not review:
                    db.rollback()
                    raise ClaimStoreError(f"Unknown review: {review_id}.")
                prior = db.execute(
                    "SELECT request_hash, result_json FROM review_decisions WHERE review_id = ? AND idempotency_key = ?",
                    (review_id, idempotency_key),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        db.rollback()
                        raise IdempotencyError(
                            "Idempotency key was already used for a different review request."
                        )
                    db.commit()
                    return json.loads(prior["result_json"])
                if review["status"] != "open":
                    db.rollback()
                    raise ClaimStoreError(f"Review {review_id} is already {review['status']}.")

                current_version = self._subject_version(db, review)
                if current_version != expected_version:
                    db.rollback()
                    raise StaleReviewError(
                        "The reviewed object changed after this review was opened; compare again before acting.",
                        current_version=current_version,
                    )

                result = self._apply_review_action(
                    db=db,
                    scope=scope,
                    review=review,
                    action=action,
                    actor_subject=actor_subject,
                    note=note,
                    edited_statement=edited_statement,
                    topic_id=topic_id,
                )
                stamp = now_iso()
                db.execute(
                    """INSERT INTO review_decisions(
                           decision_id, review_id, action, actor_subject, note, expected_version,
                           idempotency_key, request_hash, result_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("rvd_"),
                        review_id,
                        action,
                        actor_subject,
                        note,
                        expected_version,
                        idempotency_key,
                        request_hash,
                        _json(result),
                        stamp,
                    ),
                )
                db.execute(
                    "UPDATE review_queue SET status = 'resolved', resolved_at = ?, resolved_by = ? WHERE review_id = ?",
                    (stamp, actor_subject, review_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return result

    @staticmethod
    def _subject_version(db: sqlite3.Connection, review: sqlite3.Row) -> str:
        if review["subject_kind"] != "claim":
            return review["subject_version"]
        row = db.execute(
            "SELECT current_version_id FROM claims WHERE claim_id = ?", (review["subject_id"],)
        ).fetchone()
        return row["current_version_id"] if row else ""

    def _apply_review_action(
        self,
        *,
        db: sqlite3.Connection,
        scope: Scope,
        review: sqlite3.Row,
        action: str,
        actor_subject: str,
        note: str,
        edited_statement: str | None,
        topic_id: str | None,
    ) -> dict[str, Any]:
        base = {
            "review_id": review["review_id"],
            "action": action,
            "subject_id": review["subject_id"],
            "actor_subject": actor_subject,
        }
        if action == "confirm_identity":
            if not topic_id:
                raise ClaimStoreError("confirm_identity needs the topic to attach.")
            # The foreign key would refuse this too. Checking first turns a
            # constraint violation into a message that names the missing topic.
            known = db.execute(
                "SELECT 1 FROM topics WHERE topic_id = ? AND knowledge_space_id = ?",
                (topic_id, scope.knowledge_space_id),
            ).fetchone()
            if not known:
                raise ClaimStoreError(
                    f"Topic {topic_id} does not exist in knowledge space {scope.knowledge_space_id}."
                )
            db.execute(
                "INSERT OR IGNORE INTO topic_claims(knowledge_space_id, project_id, topic_id, claim_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (scope.knowledge_space_id, scope.project_id, topic_id, review["subject_id"], now_iso()),
            )
            return {**base, "topic_id": topic_id}

        if action == "confirm_supersession":
            candidates = json.loads(review["candidates_json"])
            replacement = candidates.get("replacement_claim_id") if isinstance(candidates, dict) else None
            if not replacement:
                raise ClaimStoreError("confirm_supersession needs a replacement claim.")
            current = db.execute(
                "SELECT current_version_id FROM claims WHERE claim_id = ? AND project_id = ?",
                (review["subject_id"], scope.project_id),
            ).fetchone()
            replacement_row = db.execute(
                "SELECT current_version_id FROM claims WHERE claim_id = ? AND project_id = ?",
                (replacement, scope.project_id),
            ).fetchone()
            if not current or not replacement_row:
                raise ClaimStoreError("Both sides of a supersession must exist in this project.")
            db.execute(
                "INSERT OR IGNORE INTO claim_relations(relation_id, knowledge_space_id, project_id, relation_type, relation_status, from_claim_version_id, to_claim_version_id, from_claim_id, to_claim_id, origin_evidence_json, recorded_by, created_at) VALUES (?, ?, ?, 'supersedes', 'accepted', ?, ?, ?, ?, '[]', ?, ?)",
                (
                    new_id("rel_"),
                    scope.knowledge_space_id,
                    scope.project_id,
                    replacement_row["current_version_id"],
                    current["current_version_id"],
                    replacement,
                    review["subject_id"],
                    actor_subject,
                    now_iso(),
                ),
            )
            db.execute(
                "UPDATE claims SET lifecycle_status = 'superseded', updated_at = ? WHERE claim_id = ?",
                (now_iso(), review["subject_id"]),
            )
            db.execute(
                "UPDATE claim_versions SET lifecycle_status = 'superseded' WHERE claim_version_id = ?",
                (current["current_version_id"],),
            )
            return {**base, "replacement_claim_id": replacement}

        if action == "adopt_decision":
            current = db.execute(
                "SELECT v.* FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id WHERE v.claim_id = ? AND v.claim_version_id = c.current_version_id",
                (review["subject_id"],),
            ).fetchone()
            if not current:
                raise ClaimStoreError("The claim to adopt does not exist.")
            if current["knowledge_kind"] != "decision":
                raise ClaimStoreError("Only a decision can be adopted.")
            new_version_id = new_id("clv_")
            version_number = int(
                db.execute(
                    "SELECT MAX(version) AS version FROM claim_versions WHERE claim_id = ?",
                    (review["subject_id"],),
                ).fetchone()["version"]
            ) + 1
            stamp = now_iso()
            db.execute(
                """INSERT INTO claim_versions(
                       claim_version_id, claim_id, knowledge_space_id, project_id, version, statement,
                       knowledge_kind, derivation, epistemic_status, lifecycle_status, grounding_status,
                       decision_state, question_state, conditions_json, asserted_by, asserted_at,
                       asserted_at_precision, valid_from, valid_to, attributes_json, created_at,
                       committed_at, actor_subject, knowledge_version)
                   SELECT ?, claim_id, knowledge_space_id, project_id, ?, statement, knowledge_kind, derivation,
                          epistemic_status, 'active', grounding_status, 'adopted', question_state,
                          conditions_json, asserted_by, asserted_at, asserted_at_precision, valid_from,
                          valid_to, attributes_json, ?, ?, ?, ?
                   FROM claim_versions WHERE claim_version_id = ?""",
                (
                    new_version_id,
                    version_number,
                    stamp,
                    stamp,
                    actor_subject,
                    self._ensure_meta(db, scope) + 1,
                    current["claim_version_id"],
                ),
            )
            attributes = json.loads(current["attributes_json"]) or {}
            adopted_by = list(attributes.get("adopted_by", []))
            if actor_subject not in adopted_by:
                adopted_by.append(actor_subject)
            attributes["adopted_by"] = adopted_by
            db.execute(
                "UPDATE claim_versions SET attributes_json = ? WHERE claim_version_id = ?",
                (_json(attributes), new_version_id),
            )
            db.execute(
                """INSERT INTO claim_origins(origin_id, claim_version_id, derivation, inference_note, assumptions_json, support_group_id, origin_status, asserted_by, asserted_at, created_at)
                   VALUES (?, ?, 'explicit', '', '[]', ?, 'active', ?, ?, ?)""",
                (new_id("org_"), new_version_id, new_id("sgr_"), actor_subject, stamp, stamp),
            )
            db.execute(
                "UPDATE claims SET current_version_id = ?, updated_at = ? WHERE claim_id = ?",
                (new_version_id, stamp, review["subject_id"]),
            )
            db.execute(
                "UPDATE knowledge_meta SET knowledge_version = knowledge_version + 1, updated_at = ? WHERE project_id = ?",
                (stamp, scope.project_id),
            )
            return {**base, "claim_version_id": new_version_id, "decision_state": "adopted"}

        if action == "edit":
            if not edited_statement or not edited_statement.strip():
                raise ClaimStoreError("edit needs the new statement.")
            return {**base, "pending_kind": "manual_note", "statement": edited_statement.strip()}

        if action == "reject":
            return {**base, "retained": False}

        if action == "retain":
            # Retention is its own axis. The decision row records it, which is what
            # keeps a later reader from reading "retained" as "verified" or
            # "adopted". Nothing else is written, so retention changes no state.
            return {
                **base,
                "retained": True,
                "epistemic_status_changed": False,
                "decision_state_changed": False,
                "derivation_changed": False,
            }

        raise ClaimStoreError(f"Unhandled review action: {action!r}.")

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        scope: Scope,
        run_id: str,
        config_fingerprint: str,
        coverage: Mapping[str, Any],
        status: str = "received",
    ) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                stamp = now_iso()
                db.execute(
                    """INSERT INTO ingest_runs(run_id, knowledge_space_id, project_id, status, config_fingerprint, coverage_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        scope.knowledge_space_id,
                        scope.project_id,
                        status,
                        config_fingerprint,
                        _json(coverage),
                        stamp,
                        stamp,
                    ),
                )
                for batch in coverage.get("batches", ()):
                    db.execute(
                        "INSERT INTO run_items(run_id, batch_id, status, source_ids_json, result_json, attempts) VALUES (?, ?, 'pending', ?, '{}', 0)",
                        (run_id, str(batch.get("batch_id", "")), _json(list(batch.get("source_ids", ())))),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"run_id": run_id, "status": status, "coverage": dict(coverage)}

    def advance_run(self, run_id: str, status: str, *, error_code: str = "") -> dict[str, Any]:
        """Move a run along its state machine, refusing an illegal jump.

        The refusal is the point. A crashed extraction that reports success
        because the next legal state was assumed is the failure this prevents.
        """

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM ingest_runs WHERE run_id = ?", (run_id,)).fetchone()
                if not row:
                    db.rollback()
                    raise ClaimStoreError(f"Unknown run: {run_id}.")
                current = row["status"]
                if status != current and status not in RUN_TRANSITIONS.get(current, frozenset()):
                    db.rollback()
                    raise ClaimStoreError(f"Illegal run transition: {current} -> {status}.")
                db.execute(
                    "UPDATE ingest_runs SET status = ?, error_code = ?, updated_at = ? WHERE run_id = ?",
                    (status, error_code or None, now_iso(), run_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"run_id": run_id, "status": status, "previous": current}

    def record_run_item(
        self,
        *,
        run_id: str,
        batch_id: str,
        status: str,
        result: Mapping[str, Any],
        attempts: int = 1,
    ) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "UPDATE run_items SET status = ?, result_json = ?, attempts = ? WHERE run_id = ? AND batch_id = ?",
                    (status, _json(dict(result)), attempts, run_id, batch_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def run_status(self, run_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM ingest_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                raise ClaimStoreError(f"Unknown run: {run_id}.")
            items = db.execute(
                "SELECT batch_id, status, source_ids_json, attempts FROM run_items WHERE run_id = ? ORDER BY batch_id",
                (run_id,),
            ).fetchall()
        return {
            "run_id": run_id,
            "status": row["status"],
            "error_code": row["error_code"],
            "committed_version": row["committed_version"],
            "review_pending": row["review_pending"],
            "coverage": json.loads(row["coverage_json"]),
            "items": [
                {
                    "batch_id": item["batch_id"],
                    "status": item["status"],
                    "source_ids": json.loads(item["source_ids_json"]),
                    "attempts": item["attempts"],
                }
                for item in items
            ],
            "unfinished": [item["batch_id"] for item in items if item["status"] != "done"],
        }

    def stage_artifact(
        self,
        *,
        run_id: str,
        stage: str,
        input_fingerprint: str,
        output: Mapping[str, Any],
        prompt_version: str = "",
        model_id: str = "",
    ) -> dict[str, Any] | None:
        """Reuse a stage output only when every input fingerprint still matches.

        A prompt or model change moves the fingerprint, so a cache hit can never
        come from a stage that would now produce something different.
        """

        with self._db() as db:
            registered = db.execute(
                "SELECT 1 FROM ingest_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not registered:
                raise ClaimStoreError(
                    f"Run {run_id!r} is not registered, so a stage output cannot be cached against it. "
                    "Call create_run first."
                )
            row = db.execute(
                """SELECT output_json, prompt_version, model_id FROM stage_artifacts
                   WHERE run_id = ? AND stage = ? AND input_fingerprint = ? AND prompt_version = ? AND model_id = ?""",
                (run_id, stage, input_fingerprint, prompt_version, model_id),
            ).fetchone()
            if row:
                return {"cached": True, "output": json.loads(row["output_json"])}
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT INTO stage_artifacts(run_id, stage, input_fingerprint, prompt_version, model_id, output_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, stage, input_fingerprint, prompt_version, model_id, _json(dict(output)), now_iso()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"cached": False, "output": dict(output)}

    def record_fingerprint(
        self,
        *,
        scope: Scope,
        fingerprint: str,
        artifact_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Remember that this exact content was already extracted in this project."""

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT artifact_id, run_id FROM content_fingerprints WHERE project_id = ? AND fingerprint = ?",
                    (scope.project_id, fingerprint),
                ).fetchone()
                if row:
                    db.commit()
                    return {
                        "seen": True,
                        "artifact_id": row["artifact_id"],
                        "first_run_id": row["run_id"],
                    }
                db.execute(
                    "INSERT INTO content_fingerprints(knowledge_space_id, project_id, fingerprint, artifact_id, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (scope.knowledge_space_id, scope.project_id, fingerprint, artifact_id, run_id, now_iso()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"seen": False, "artifact_id": artifact_id, "first_run_id": run_id}

    def evidence_lineage(self, evidence_id: str, scope: Scope) -> dict[str, Any]:
        """Where one citation's text came from, and which content it carries.

        Two citations of the same article, or of a page summarising one
        conversation, are the same material seen twice. Comparing this is what
        keeps a repost from reading as a second witness.
        """

        with self._db() as db:
            row = db.execute(
                """SELECT e.evidence_id, e.artifact_id, e.withdrawn_at AS evidence_withdrawn,
                          a.normalized_sha256, a.revision_id, a.parse_quality,
                          r.source_id, r.ordinal, r.withdrawn_at AS revision_withdrawn
                   FROM evidence_refs e
                   JOIN parsed_artifacts a ON a.artifact_id = e.artifact_id
                   JOIN source_revisions r ON r.revision_id = a.revision_id
                   WHERE e.evidence_id = ? AND e.project_id = ?""",
                (evidence_id, scope.project_id),
            ).fetchone()
        if not row:
            raise ClaimStoreError(f"Unknown evidence in project {scope.project_id}: {evidence_id}.")
        return {
            "evidence_id": row["evidence_id"],
            "artifact_id": row["artifact_id"],
            "content_fingerprint": row["normalized_sha256"],
            "revision_id": row["revision_id"],
            "source_id": row["source_id"],
            "source_ordinal": int(row["ordinal"]),
            "parse_quality": row["parse_quality"],
            "available": row["evidence_withdrawn"] is None and row["revision_withdrawn"] is None,
        }

    def support_lineage(self, claim_version_id: str, scope: Scope) -> dict[str, Any]:
        """The witness count behind one claim version, with reposts counted once.

        Support groups are alternatives, so two groups normally mean two ways to
        hold the claim. When both groups rest on the same content, they are one
        witness seen twice, and this reports that rather than letting the count
        stand as evidence of corroboration.
        """

        with self._db() as db:
            rows = db.execute(
                """SELECT r.support_group_id, r.requirement_kind, r.requirement_id, r.available
                   FROM claim_support_requirements r
                   JOIN claim_versions v ON v.claim_version_id = r.claim_version_id
                   WHERE r.claim_version_id = ? AND v.project_id = ?
                   ORDER BY r.support_group_id, r.position""",
                (claim_version_id, scope.project_id),
            ).fetchall()
        if not rows:
            raise ClaimStoreError(f"Unknown claim version in project {scope.project_id}: {claim_version_id}.")

        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["support_group_id"], []).append(
                {"kind": row["requirement_kind"], "id": row["requirement_id"], "available": bool(row["available"])}
            )

        described: list[dict[str, Any]] = []
        for group_id, requirements in groups.items():
            fingerprints: list[str] = []
            unavailable: list[str] = []
            for requirement in requirements:
                if requirement["kind"] == "evidence":
                    lineage = self.evidence_lineage(requirement["id"], scope)
                    fingerprints.append(lineage["content_fingerprint"])
                    if not lineage["available"]:
                        unavailable.append(requirement["id"])
                else:
                    # A premise claim is its own witness, so it is keyed by its own
                    # version rather than by any text fingerprint.
                    fingerprints.append(f"premise:{requirement['id']}")
                    if not requirement["available"]:
                        unavailable.append(requirement["id"])
            described.append(
                {
                    "support_group_id": group_id,
                    "requirements": requirements,
                    "content_fingerprints": fingerprints,
                    "distinct_content": len(set(fingerprints)),
                    "duplicated_content": len(fingerprints) - len(set(fingerprints)),
                    "unavailable": unavailable,
                    "intact": not unavailable and bool(fingerprints),
                }
            )

        # Two groups are the same witness when their content sets are equal.
        witness_keys = {tuple(sorted(group["content_fingerprints"])) for group in described}
        return {
            "claim_version_id": claim_version_id,
            "groups": described,
            "group_count": len(described),
            "independent_witnesses": len(witness_keys),
            "repost_groups": max(0, len(described) - len(witness_keys)),
            "intact_groups": sum(1 for group in described if group["intact"]),
            "grounding_status": (
                "grounded"
                if any(group["intact"] for group in described)
                else ("needs_revalidation" if any(group["requirements"] for group in described) else "unsupported")
            ),
        }

    def reposted_sources(self, scope: Scope) -> list[dict[str, Any]]:
        """Sources in this project that carry content another source already carries.

        Reported rather than acted on: a repost is legitimate material, and the
        thing that must not happen is counting it as a second independent witness.
        """

        with self._db() as db:
            rows = db.execute(
                """SELECT a.normalized_sha256 AS fingerprint,
                          GROUP_CONCAT(DISTINCT r.source_id) AS sources,
                          COUNT(DISTINCT r.source_id) AS source_count
                   FROM parsed_artifacts a
                   JOIN source_revisions r ON r.revision_id = a.revision_id
                   WHERE a.project_id = ?
                   GROUP BY a.normalized_sha256
                   HAVING source_count > 1
                   ORDER BY fingerprint""",
                (scope.project_id,),
            ).fetchall()
        return [
            {
                "content_fingerprint": row["fingerprint"],
                "source_ids": sorted(str(row["sources"]).split(",")),
                "source_count": int(row["source_count"]),
            }
            for row in rows
        ]

    def dispositions(self, scope: Scope, *, run_id: str = "") -> list[dict[str, Any]]:
        """Every recorded disposition, with its reason codes, so a DROP is traceable."""

        query = "SELECT * FROM dispositions WHERE project_id = ?"
        parameters: list[Any] = [scope.project_id]
        if run_id:
            query += " AND run_id = ?"
            parameters.append(run_id)
        query += " ORDER BY created_at, disposition_id"
        with self._db() as db:
            rows = db.execute(query, tuple(parameters)).fetchall()
        return [
            {
                "disposition_id": row["disposition_id"],
                "run_id": row["run_id"],
                "unit_id": row["unit_id"],
                "statement": row["statement"],
                "disposition": row["disposition"],
                "reason_codes": json.loads(row["reason_codes_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def project_snapshot(self, scope: Scope) -> dict[str, Any]:
        version = self.current_version(scope.project_id)
        claims = self.iter_claims(scope)
        return {
            "project_id": scope.project_id,
            "knowledge_space_id": scope.knowledge_space_id,
            "knowledge_version": version,
            "claim_count": len(claims),
            "claims": claims,
        }
