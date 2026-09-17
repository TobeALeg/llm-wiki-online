"""Move a v1 Wiki database into the v2 knowledge layer, and prove the copy is real.

Four rules shape this module.

The v1 database is opened with `mode=ro` and is never written, because the
migration is a copy of a record, not a rewrite of it.

Nothing migrated is verified and no migrated decision is adopted. A migration
cannot know what a person confirmed later, so a state that says otherwise is a
claim no reader can check. An extractor that returns one is refused rather than
quietly downgraded, the same way `knowledge_types.validate_claim_state` refuses
to soften a claim.

A v1 page body is generated text. It is imported as `legacy_generated_page`,
marked `legacy_unverified`, and given no evidence reference at all, so the
citation path cannot recover a quote from it. The raw material a page cites is
re-extracted through `freeze_revision` into artifacts and evidence, and that is
where quotations come from.

Missing raw material is reported. A page that cites a source the v1 database
does not contain, or a source whose stored text is empty, appears in `gaps` with
the reason instead of being filled in with the page body.

Reentrance has two layers. `legacy_map` records the v2 identity of every object
the migration created, so a second run skips what the first one wrote and an
interrupted run resumes from the checkpoint in `migration_runs`. Under that,
every individual write is idempotent on its own: a revision reuses its content
hash, an artifact id derives from its parse inputs, evidence registration
ignores duplicates, and a claim batch carries a stable idempotency key. A crash
between two steps therefore costs a repeat of those steps, never a duplicate
object.

Standard library only, and no sibling import by package name, so the same bytes
work as the canonical module and as the vendored copy inside the skill package.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .chunking import MAX_CHUNK_CHARS, TARGET_CHUNK_CHARS, artifact_structure, chunk_text
    from .claim_store import ClaimStore, ClaimStoreError
    from .evidence import EvidenceError, EvidenceRecord, freeze_artifact, make_evidence, normalize_text
    from .knowledge_types import PROJECT_ID_PATTERN, ClaimState, KnowledgeError, Scope, build_change_set
except ImportError:  # pragma: no cover - the packaged layout
    from chunking import (  # type: ignore[no-redef]
        MAX_CHUNK_CHARS,
        TARGET_CHUNK_CHARS,
        artifact_structure,
        chunk_text,
    )
    from claim_store import ClaimStore, ClaimStoreError  # type: ignore[no-redef]
    from evidence import (  # type: ignore[no-redef]
        EvidenceError,
        EvidenceRecord,
        freeze_artifact,
        make_evidence,
        normalize_text,
    )
    from knowledge_types import (  # type: ignore[no-redef]
        PROJECT_ID_PATTERN,
        ClaimState,
        KnowledgeError,
        Scope,
        build_change_set,
    )

DEFAULT_V1_PROJECT = "company"
LEGACY_PAGE_PARSE_QUALITY = "legacy_generated_page"
LEGACY_PAGE_SOURCE_TYPE = "legacy_page"
SOURCE_PARSER_NAME = "legacy_v1_source"
SOURCE_PARSER_VERSION = "1"
PAGE_PARSER_NAME = "legacy_v1_page"
PAGE_PARSER_VERSION = "1"
DEFAULT_PROJECT_NAME = "Company Wiki"

CLAIMS_SKIPPED_WITHOUT_EXTRACTOR = "extractor_not_provided"
"""Set on the report when no extractor was supplied. Silence here would read as
"the sources held nothing", which is a different and unproven statement."""

SOURCE_PARSER_QUALITY = "ok"
GENERATED_PAGE_NOTE = (
    "Imported from a v1 page body. The text was generated, not captured from raw "
    "material, so it is not first-hand evidence and carries no citation."
)

MIGRATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS migration_runs (
    knowledge_space_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint INTEGER,
    cursor_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, run_id)
);

CREATE TABLE IF NOT EXISTS legacy_map (
    knowledge_space_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    legacy_kind TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    new_kind TEXT NOT NULL,
    new_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_space_id, project_id, legacy_kind, legacy_id)
);
CREATE INDEX IF NOT EXISTS legacy_map_new ON legacy_map(new_kind, new_id);
"""
"""Progress and identity, both owned by this module rather than by `claim_store`.

`migration_runs` holds the checkpoint a resumed run reads and the report it last
produced. `legacy_map` holds the deterministic v1 identity to v2 identity
mapping, which is what makes a second run a no-op instead of a second copy.
"""

_WRITE_TABLES = (
    ("sources", "source_id"),
    ("source_revisions", "revision_id"),
    ("parsed_artifacts", "artifact_id"),
    ("evidence_refs", "evidence_id"),
    ("topics", "topic_id"),
    ("claims", "claim_id"),
    ("claim_versions", "claim_version_id"),
    ("claim_relations", "relation_id"),
    ("page_projections", "page_id"),
    ("review_queue", "review_id"),
    ("review_decisions", "decision_id"),
)
"""What a rollback has to account for. Ids only: a row that was not in the backup
and is in the live database is a write made after the backup, whatever it says."""


class MigrationError(RuntimeError):
    """A migration that cannot proceed honestly."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only_uri(path: Path) -> str:
    """A `file:` URI that can read a database but cannot create or write one."""

    posix = path.resolve().as_posix()
    if not posix.startswith("/"):
        # Windows reports a drive path without the leading slash the URI form needs.
        posix = "/" + posix
    return "file:" + posix + "?mode=ro"


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(_read_only_uri(path), uri=True, timeout=10, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _open_v1(database: str | Path) -> sqlite3.Connection:
    path = Path(database)
    if not path.is_file():
        raise MigrationError(f"The v1 database does not exist: {path}.")
    connection = _connect(path, read_only=True)
    if not _has_table(connection, "wiki_pages") or not _has_table(connection, "wiki_sources"):
        connection.close()
        raise MigrationError(
            f"{path} is not a v1 Wiki database: wiki_pages and wiki_sources are not both present."
        )
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
    )


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(connection, table):
        return False
    return column in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _identity_digest(kind: str, project_id: str, legacy_id: str) -> str:
    payload = f"legacy-v1:{kind}:{project_id}:{legacy_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def legacy_source_id(project_id: str, v1_source_id: str) -> str:
    """The v2 source id one v1 source maps to, derived from its identity alone.

    Deriving it from (project, legacy id) instead of minting it means a second
    run, or a run that lost its map, addresses the same v2 source rather than a
    second copy of it.
    """

    return "src_" + _identity_digest("source", project_id, v1_source_id)


def legacy_page_source_id(project_id: str, slug: str) -> str:
    """The v2 source id a v1 page body is recorded under."""

    return "src_" + _identity_digest("page", project_id, slug)


def _config_hash() -> str:
    payload = {
        "parser": SOURCE_PARSER_NAME,
        "source_version": SOURCE_PARSER_VERSION,
        "page_parser": PAGE_PARSER_NAME,
        "page_version": PAGE_PARSER_VERSION,
        "target_chars": TARGET_CHUNK_CHARS,
        "max_chars": MAX_CHUNK_CHARS,
    }
    return "sha256:" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _claim_key(knowledge_space_id: str, project_id: str, v1_source_id: str) -> str:
    digest = hashlib.sha256(
        f"legacy-claims:{knowledge_space_id}:{project_id}:{v1_source_id}".encode("utf-8")
    ).hexdigest()
    return "migrate-" + digest[:48]


def _has_raw_text(source: Mapping[str, Any]) -> bool:
    return bool(str(source.get("content") or "").strip())


def _cited_source_ids(page: Mapping[str, Any]) -> tuple[list[str], bool]:
    """The source ids a page names, and whether its list could be read at all."""

    raw = page.get("source_ids_json")
    if raw is None:
        return [], True
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return [], False
    if not isinstance(values, list):
        return [], False
    found: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in found:
            found.append(text)
    return found, True


class _V1Reader:
    """Reads the v1 tables, with or without the project column.

    The oldest v1 files predate project scoping and hold one implicit project.
    Reading that shape here means the migration never has to open the v1
    database read-write to convert it first.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.scoped = _has_column(connection, "wiki_pages", "project_id")
        self._sources: dict[str, list[dict[str, Any]]] = {}
        self._pages: dict[str, list[dict[str, Any]]] = {}

    def close(self) -> None:
        self.connection.close()

    def projects(self, only: str | None = None) -> list[dict[str, Any]]:
        if not self.scoped:
            found = [
                {
                    "project_id": DEFAULT_V1_PROJECT,
                    "name": DEFAULT_PROJECT_NAME,
                    "created_at": "",
                    "version": self._legacy_version(),
                }
            ]
        else:
            if not _has_table(self.connection, "wiki_projects"):
                raise MigrationError(
                    "This v1 database scopes pages by project but has no wiki_projects table."
                )
            rows = self.connection.execute(
                "SELECT id, name, created_at FROM wiki_projects ORDER BY id"
            ).fetchall()
            found = [
                {
                    "project_id": row["id"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "version": self._project_version(row["id"]),
                }
                for row in rows
            ]
        if only is None:
            return found
        wanted = str(only).strip().lower()
        selected = [item for item in found if item["project_id"] == wanted]
        if not selected:
            raise MigrationError(f"Unknown v1 project: {wanted}.")
        return selected

    def _legacy_version(self) -> int:
        if not _has_table(self.connection, "wiki_meta"):
            return 0
        row = self.connection.execute(
            "SELECT value FROM wiki_meta WHERE key = 'current_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _project_version(self, project_id: str) -> int:
        if _has_table(self.connection, "wiki_project_meta"):
            row = self.connection.execute(
                "SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row:
                return int(row["current_version"])
        return 0

    def sources(self, project_id: str) -> list[dict[str, Any]]:
        if project_id in self._sources:
            return self._sources[project_id]
        if not self.scoped:
            if project_id != DEFAULT_V1_PROJECT:
                self._sources[project_id] = []
                return self._sources[project_id]
            rows = self.connection.execute(
                "SELECT source_id, kind, label, content, created_at FROM wiki_sources ORDER BY source_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT source_id, kind, label, content, created_at FROM wiki_sources
                   WHERE project_id = ? ORDER BY source_id""",
                (project_id,),
            ).fetchall()
        self._sources[project_id] = [dict(row) for row in rows]
        return self._sources[project_id]

    def source(self, project_id: str, source_id: str) -> dict[str, Any] | None:
        for row in self.sources(project_id):
            if row["source_id"] == source_id:
                return row
        return None

    def pages(self, project_id: str) -> list[dict[str, Any]]:
        if project_id in self._pages:
            return self._pages[project_id]
        columns = (
            "slug, title, type, status, tags_json, summary, body, source_ids_json, updated_at, version"
        )
        if not self.scoped:
            if project_id != DEFAULT_V1_PROJECT:
                self._pages[project_id] = []
                return self._pages[project_id]
            rows = self.connection.execute(
                f"SELECT {columns} FROM wiki_pages ORDER BY slug"
            ).fetchall()
        else:
            rows = self.connection.execute(
                f"SELECT {columns} FROM wiki_pages WHERE project_id = ? ORDER BY slug",
                (project_id,),
            ).fetchall()
        self._pages[project_id] = [dict(row) for row in rows]
        return self._pages[project_id]

    def page(self, project_id: str, slug: str) -> dict[str, Any] | None:
        for row in self.pages(project_id):
            if row["slug"] == slug:
                return row
        return None

    def history(self, project_id: str, slug: str) -> dict[str, Any]:
        """The v1 version rows for one page, with the audit summary v1 itself pairs them with.

        `page_versions` in `store.py` joins an audit to a version on (project,
        version, action), so the same join is what keeps the two lists aligned
        here rather than a guess about which audit belongs to which page.
        """

        where, params = ("WHERE v.slug = ?", (slug,))
        if self.scoped:
            where = "WHERE v.project_id = ? AND v.slug = ?"
            params = (project_id, slug)
        rows = self.connection.execute(
            f"""SELECT v.id, v.version, v.action, v.actor_subject, v.created_at,
                       v.previous_version, a.summary AS audit_summary
                FROM wiki_versions AS v
                LEFT JOIN wiki_audits AS a
                    ON a.version = v.version AND a.action = v.action
                    {'AND a.project_id = v.project_id' if self.scoped else ''}
                {where}
                ORDER BY v.version, v.id""",
            params,
        ).fetchall()
        return {"versions": [dict(row) for row in rows]}

    def audit_count(self, project_id: str) -> int:
        if not _has_table(self.connection, "wiki_audits"):
            return 0
        if not self.scoped:
            row = self.connection.execute("SELECT COUNT(*) AS total FROM wiki_audits").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM wiki_audits WHERE project_id = ?", (project_id,)
            ).fetchone()
        return int(row["total"])


def _v1_legacy_refs(reader: _V1Reader, projects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every page-to-source reference the v1 database makes, and whether it resolves."""

    refs: list[dict[str, Any]] = []
    for project in projects:
        project_id = str(project["project_id"])
        for page in reader.pages(project_id):
            cited, readable = _cited_source_ids(page)
            for source_id in cited:
                source = reader.source(project_id, source_id)
                refs.append(
                    {
                        "project_id": project_id,
                        "legacy_kind": "page",
                        "legacy_id": page["slug"],
                        "source_id": source_id,
                        "exists": source is not None,
                        "has_raw_text": bool(source is not None and _has_raw_text(source)),
                        "readable": readable,
                    }
                )
    return refs


def _v1_gaps(reader: _V1Reader, projects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Raw material v1 lost or never had, named one row at a time."""

    gaps: list[dict[str, Any]] = []
    for project in projects:
        project_id = str(project["project_id"])
        sources = {str(row["source_id"]): row for row in reader.sources(project_id)}
        for source_id in sorted(sources):
            if not _has_raw_text(sources[source_id]):
                gaps.append(
                    {
                        "project_id": project_id,
                        "legacy_kind": "source",
                        "legacy_id": source_id,
                        "source_id": source_id,
                        "kind": "source_text_empty",
                        "reason": "The v1 source row exists but stores no text, so there is nothing to re-extract.",
                    }
                )
        for page in reader.pages(project_id):
            cited, readable = _cited_source_ids(page)
            if not readable:
                gaps.append(
                    {
                        "project_id": project_id,
                        "legacy_kind": "page",
                        "legacy_id": page["slug"],
                        "source_id": "",
                        "kind": "page_source_ids_unreadable",
                        "reason": "The page's source_ids_json could not be read, so its citations are unknown.",
                    }
                )
                continue
            if not cited:
                gaps.append(
                    {
                        "project_id": project_id,
                        "legacy_kind": "page",
                        "legacy_id": page["slug"],
                        "source_id": "",
                        "kind": "page_without_raw_material",
                        "reason": "The page cites no source, so only generated text exists behind it.",
                    }
                )
                continue
            for source_id in cited:
                source = sources.get(source_id)
                if source is None:
                    gaps.append(
                        {
                            "project_id": project_id,
                            "legacy_kind": "page",
                            "legacy_id": page["slug"],
                            "source_id": source_id,
                            "kind": "page_source_missing",
                            "reason": "The page cites a source the v1 database does not contain.",
                        }
                    )
                elif not _has_raw_text(source):
                    gaps.append(
                        {
                            "project_id": project_id,
                            "legacy_kind": "page",
                            "legacy_id": page["slug"],
                            "source_id": source_id,
                            "kind": "page_source_empty_text",
                            "reason": "The page cites a source whose stored text is empty.",
                        }
                    )
    return sorted(
        gaps,
        key=lambda gap: (
            gap["project_id"],
            gap["legacy_kind"],
            gap["legacy_id"],
            gap["kind"],
            gap["source_id"],
        ),
    )


def plan_migration(*, v1_database: str | Path, knowledge_space_id: str, project_id: str | None = None) -> dict[str, Any]:
    """Say what a migration would carry, reading the v1 database without writing it.

    The connection is opened as `mode=ro`, so a dry run cannot create the file,
    cannot checkpoint it, and cannot modify a row even if this function is wrong.
    """

    space = str(knowledge_space_id or "").strip()
    if not space:
        raise MigrationError("A knowledge space id is required.")
    reader = _V1Reader(_open_v1(v1_database))
    try:
        projects = reader.projects(project_id)
        sources = [
            (str(project["project_id"]), row)
            for project in projects
            for row in reader.sources(str(project["project_id"]))
        ]
        pages = [
            (str(project["project_id"]), row)
            for project in projects
            for row in reader.pages(str(project["project_id"]))
        ]
        with_raw = sum(1 for _, row in sources if _has_raw_text(row))
        generated_only = 0
        for project_id_value, page in pages:
            cited, readable = _cited_source_ids(page)
            if not readable or not cited:
                generated_only += 1
                continue
            backed = any(
                (source := reader.source(project_id_value, source_id)) is not None
                and _has_raw_text(source)
                for source_id in cited
            )
            if not backed:
                generated_only += 1
        return {
            "v1_database": str(Path(v1_database)),
            "knowledge_space_id": space,
            "project_filter": project_id,
            "projects": [
                {
                    "project_id": str(project["project_id"]),
                    "name": str(project["name"]),
                    "version": int(project["version"]),
                    "sources": sum(
                        1 for pid, _ in sources if pid == str(project["project_id"])
                    ),
                    "pages": sum(1 for pid, _ in pages if pid == str(project["project_id"])),
                }
                for project in projects
            ],
            "sources": {
                "total": len(sources),
                "with_raw_text": with_raw,
                "without_raw_text": len(sources) - with_raw,
            },
            "pages": {"total": len(pages), "generated_only": generated_only},
            "legacy_refs": _v1_legacy_refs(reader, projects),
            "gaps": _v1_gaps(reader, projects),
        }
    finally:
        reader.close()


class _Migration:
    """One migration call: the v1 work list, the v2 writes, and the report."""

    def __init__(
        self,
        *,
        reader: _V1Reader,
        v2_database: Path,
        knowledge_space_id: str,
        actor_subject: str,
        run_id: str,
        checkpoint: int | None,
        extractor: Callable[[dict[str, Any]], list[dict[str, Any]]] | None,
    ):
        self.reader = reader
        self.knowledge_space_id = knowledge_space_id
        self.actor_subject = actor_subject
        self.run_id = run_id
        self.checkpoint = checkpoint
        self.extractor = extractor
        self.projects = reader.projects()
        for project in self.projects:
            if not PROJECT_ID_PATTERN.fullmatch(str(project["project_id"])):
                raise MigrationError(
                    f"v1 project {project['project_id']!r} is not a legal v2 project id."
                )
        self.connection = _connect(v2_database)
        self.connection.executescript(MIGRATION_SCHEMA_SQL)
        self.store = ClaimStore(v2_database, knowledge_space_id=knowledge_space_id)
        self.migrated_sources = 0
        self.resume = "fresh"
        self._ensured: set[str] = set()
        if self.projects:
            # Fails on a malformed knowledge space id before anything is written.
            self._scope(str(self.projects[0]["project_id"]))

    def close(self) -> None:
        self.connection.close()

    def _scope(self, project_id: str) -> Scope:
        try:
            return Scope.of(self.knowledge_space_id, project_id)
        except KnowledgeError as error:
            raise MigrationError(str(error)) from error

    # ------------------------------------------------------------------
    # Work list and progress
    # ------------------------------------------------------------------

    def _full_work_list(self) -> tuple[list[list[str]], list[list[str]]]:
        sources: list[list[str]] = []
        pages: list[list[str]] = []
        for project in self.projects:
            project_id = str(project["project_id"])
            for row in self.reader.sources(project_id):
                if _has_raw_text(row):
                    sources.append([project_id, str(row["source_id"])])
            for row in self.reader.pages(project_id):
                pages.append([project_id, str(row["slug"])])
        return sources, pages

    def _fingerprint(self) -> str:
        items: list[list[Any]] = []
        for project in self.projects:
            project_id = str(project["project_id"])
            for row in self.reader.sources(project_id):
                items.append(["source", project_id, str(row["source_id"]), _has_raw_text(row)])
            for row in self.reader.pages(project_id):
                items.append(["page", project_id, str(row["slug"])])
        return "sha256:" + hashlib.sha256(_json(items).encode("utf-8")).hexdigest()

    def _stored_cursor(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT cursor_json FROM migration_runs WHERE knowledge_space_id = ? AND run_id = ?",
            (self.knowledge_space_id, self.run_id),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["cursor_json"])
        except ValueError:
            return None

    def _work_list(self) -> tuple[list[list[str]], list[list[str]]]:
        """Where this call starts: the stored checkpoint, or a fresh scan of v1."""

        stored = self._stored_cursor()
        fingerprint = self._fingerprint()
        if stored and stored.get("v1_fingerprint") == fingerprint:
            self.resume = "cursor"
            return (
                [list(item) for item in stored.get("pending_sources", [])],
                [list(item) for item in stored.get("pending_pages", [])],
            )
        self.resume = "fresh" if stored is None else "recomputed"
        return self._full_work_list()

    def _pending(self) -> tuple[list[list[str]], list[list[str]]]:
        sources, pages = self._full_work_list()
        remaining_sources = [
            item for item in sources if self._mapped(item[0], "source", item[1]) is None
        ]
        remaining_pages = [item for item in pages if self._mapped(item[0], "page", item[1]) is None]
        return remaining_sources, remaining_pages

    def _record_run(
        self, *, status: str, cursor: Mapping[str, Any] | None, report: Mapping[str, Any] | None
    ) -> None:
        stamp = _now_iso()
        current = self.connection.execute(
            "SELECT cursor_json, report_json FROM migration_runs WHERE knowledge_space_id = ? AND run_id = ?",
            (self.knowledge_space_id, self.run_id),
        ).fetchone()
        cursor_json = _json(cursor) if cursor is not None else (current["cursor_json"] if current else "{}")
        report_json = (
            _json(report) if report is not None else (current["report_json"] if current else "{}")
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """INSERT INTO migration_runs(
                       knowledge_space_id, run_id, status, checkpoint, cursor_json, report_json,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(knowledge_space_id, run_id) DO UPDATE SET
                       status = excluded.status,
                       checkpoint = excluded.checkpoint,
                       cursor_json = excluded.cursor_json,
                       report_json = excluded.report_json,
                       updated_at = excluded.updated_at""",
                (
                    self.knowledge_space_id,
                    self.run_id,
                    status,
                    self.checkpoint,
                    cursor_json,
                    report_json,
                    stamp,
                    stamp,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def mark_failed(self) -> None:
        # Reporting the failure must not replace the failure.
        with suppress(sqlite3.Error):
            self._record_run(status="failed", cursor=None, report=None)

    # ------------------------------------------------------------------
    # Identity mapping
    # ------------------------------------------------------------------

    def _mapped(self, project_id: str, legacy_kind: str, legacy_id: str) -> str | None:
        row = self.connection.execute(
            """SELECT new_id FROM legacy_map
               WHERE knowledge_space_id = ? AND project_id = ? AND legacy_kind = ? AND legacy_id = ?""",
            (self.knowledge_space_id, project_id, legacy_kind, legacy_id),
        ).fetchone()
        return str(row["new_id"]) if row else None

    def _mapping_count(self, project_id: str | None, legacy_kind: str) -> int:
        query = "SELECT COUNT(*) AS total FROM legacy_map WHERE knowledge_space_id = ? AND legacy_kind = ?"
        parameters: list[Any] = [self.knowledge_space_id, legacy_kind]
        if project_id is not None:
            query += " AND project_id = ?"
            parameters.append(project_id)
        return int(self.connection.execute(query, tuple(parameters)).fetchone()["total"])

    def _record_mapping(self, project_id: str, rows: Sequence[tuple[str, str, str, str]]) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for legacy_kind, legacy_id, new_kind, new_id in rows:
                self.connection.execute(
                    """INSERT OR IGNORE INTO legacy_map(
                           knowledge_space_id, project_id, legacy_kind, legacy_id, new_kind, new_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.knowledge_space_id,
                        project_id,
                        legacy_kind,
                        legacy_id,
                        new_kind,
                        new_id,
                        _now_iso(),
                    ),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _ensure_project(self, project: Mapping[str, Any]) -> str:
        project_id = str(project["project_id"])
        if project_id in self._ensured:
            return project_id
        stamp = _now_iso()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """INSERT OR IGNORE INTO knowledge_projects(knowledge_space_id, project_id, name, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    self.knowledge_space_id,
                    project_id,
                    str(project["name"]),
                    str(project.get("created_at") or "") or stamp,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        # The version counter row is what a later read looks for; creating it here
        # keeps a project with no claims readable instead of absent.
        self.store.current_version(project_id)
        self._record_mapping(project_id, [("project", project_id, "knowledge_project", project_id)])
        self._ensured.add(project_id)
        return project_id

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        for project in self.projects:
            self._ensure_project(project)
        pending_sources, pending_pages = self._work_list()
        interrupted = False
        for project_id, v1_source_id in pending_sources:
            if self._mapped(project_id, "source", v1_source_id) is not None:
                continue
            if self.checkpoint is not None and self.migrated_sources >= self.checkpoint:
                interrupted = True
                break
            self._migrate_source(project_id, v1_source_id)
            self.migrated_sources += 1
        if not interrupted:
            for project_id, slug in pending_pages:
                if self._mapped(project_id, "page", slug) is not None:
                    continue
                self._migrate_page(project_id, slug)
        remaining_sources, remaining_pages = self._pending()
        complete = not remaining_sources and not remaining_pages
        status = "completed" if complete else "checkpointed"
        safety = self._assert_nothing_auto_verified()
        report = self._report(
            status=status, safety=safety, pending_sources=remaining_sources, pending_pages=remaining_pages
        )
        self._record_run(
            status=status,
            cursor={
                "v1_fingerprint": self._fingerprint(),
                "pending_sources": remaining_sources,
                "pending_pages": remaining_pages,
                "migrated_sources": self.migrated_sources,
            },
            report=report,
        )
        return report

    def _migrate_source(self, project_id: str, v1_source_id: str) -> None:
        row = self.reader.source(project_id, v1_source_id)
        if row is None or not _has_raw_text(row):
            return
        scope = self._scope(project_id)
        content = str(row["content"])
        source_id = legacy_source_id(project_id, v1_source_id)
        revision = self.store.freeze_revision(
            scope=scope,
            source_id=source_id,
            source_type=str(row["kind"]) or "legacy",
            label=str(row["label"]) or v1_source_id,
            raw_content=content,
            captured_at=str(row["created_at"]) or None,
            raw_available=True,
            origin_uri=f"legacy-v1:{project_id}:{v1_source_id}",
        )
        normalized = normalize_text(content)
        artifact = freeze_artifact(
            revision_id=str(revision["revision_id"]),
            text=content,
            parser_name=SOURCE_PARSER_NAME,
            parser_version=SOURCE_PARSER_VERSION,
            config_hash=_config_hash(),
            structure=artifact_structure(normalized),
            parse_quality=SOURCE_PARSER_QUALITY,
        )
        chunks = chunk_text(normalized)
        self.store.store_artifact(scope=scope, artifact=artifact, chunks=chunks)
        claim_ids: list[str] = []
        if self.extractor is not None:
            claim_ids = self._commit_claims(
                project_id=project_id,
                scope=scope,
                v1_source_id=v1_source_id,
                source_id=source_id,
                revision_id=str(revision["revision_id"]),
                artifact=artifact,
                chunks=chunks,
            )
        rows: list[tuple[str, str, str, str]] = [
            ("source", v1_source_id, "source", source_id),
            ("source_revision", v1_source_id, "revision", str(revision["revision_id"])),
            ("source_artifact", v1_source_id, "artifact", artifact.artifact_id),
        ]
        rows.extend(
            ("claim", f"{v1_source_id}#{index}", "claim", claim_id)
            for index, claim_id in enumerate(claim_ids, start=1)
        )
        self._record_mapping(project_id, rows)

    def _commit_claims(
        self,
        *,
        project_id: str,
        scope: Scope,
        v1_source_id: str,
        source_id: str,
        revision_id: str,
        artifact: Any,
        chunks: Sequence[Any],
    ) -> list[str]:
        context = {
            "scope": scope,
            "source_id": source_id,
            "revision_id": revision_id,
            "artifact": artifact,
            "chunks": chunks,
            "project_id": project_id,
        }
        extracted = self.extractor(context)
        if isinstance(extracted, (list, tuple)):
            claims = [dict(claim) for claim in extracted]
        else:
            raise MigrationError(
                f"The extractor returned {type(extracted).__name__} for source {v1_source_id}; "
                "a list of claim dicts is required."
            )
        if not claims:
            return []
        for claim in claims:
            _refuse_self_granted_state(claim, v1_source_id)
        citations = {
            record.evidence_id: record
            for record in (
                chunk_evidence(project_id=project_id, artifact=artifact, chunk=chunk)
                for chunk in chunks
            )
        }
        cited: set[str] = set()
        for claim in claims:
            for origin in claim.get("origins") or ():
                for evidence_id in origin.get("evidence_refs") or ():
                    cited.add(str(evidence_id))
        unknown = sorted(cited - set(citations))
        if unknown:
            raise MigrationError(
                f"The extractor cited evidence the migration never registered for source "
                f"{v1_source_id}: {', '.join(unknown)}. Build citations with "
                "migrate_v2.chunk_evidence so the store can register them."
            )
        for evidence_id in sorted(cited):
            self.store.register_evidence(citations[evidence_id], scope=scope)
        base_version = self.store.current_version(project_id)
        changeset = build_change_set(
            knowledge_space_id=self.knowledge_space_id,
            project_id=project_id,
            run_id=self.run_id,
            claims=claims,
            base_version=base_version,
        )
        outcome = self.store.commit_changes(
            actor_subject=self.actor_subject,
            base_version=base_version,
            idempotency_key=_claim_key(self.knowledge_space_id, project_id, v1_source_id),
            changeset=changeset,
            run_id=self.run_id,
            project_id=project_id,
        )
        return [*outcome.created_claim_ids, *outcome.updated_claim_ids]

    def _migrate_page(self, project_id: str, slug: str) -> None:
        page = self.reader.page(project_id, slug)
        if page is None:
            return
        scope = self._scope(project_id)
        body = str(page["body"])
        source_id = legacy_page_source_id(project_id, slug)
        cited, readable = _cited_source_ids(page)
        raw_sources = [
            source_id_value
            for source_id_value in cited
            if (source := self.reader.source(project_id, source_id_value)) is not None
            and _has_raw_text(source)
        ]
        markers = {
            "legacy_unverified": True,
            "legacy_kind": LEGACY_PAGE_PARSE_QUALITY,
            "legacy_origin": f"legacy-v1:{project_id}:page:{slug}",
            "legacy_project_id": project_id,
            "legacy_slug": slug,
            "legacy_version": int(page["version"]),
            "legacy_source_ids": cited if readable else [],
            "legacy_citations_readable": readable,
            "legacy_raw_sources": raw_sources,
            "legacy_history": self.reader.history(project_id, slug),
            "legacy_note": GENERATED_PAGE_NOTE,
        }
        revision = self.store.freeze_revision(
            scope=scope,
            source_id=source_id,
            source_type=LEGACY_PAGE_SOURCE_TYPE,
            label=str(page["title"]) or slug,
            raw_content=body,
            captured_at=str(page["updated_at"]) or None,
            raw_available=False,
            origin_uri=f"legacy-v1:{project_id}:page:{slug}",
        )
        artifact = freeze_artifact(
            revision_id=str(revision["revision_id"]),
            text=body,
            parser_name=PAGE_PARSER_NAME,
            parser_version=PAGE_PARSER_VERSION,
            config_hash=_config_hash(),
            structure={**artifact_structure(normalize_text(body)), **markers},
            parse_quality=LEGACY_PAGE_PARSE_QUALITY,
        )
        # No chunks and no evidence reference: nothing can be handed to a model as
        # a quotation from a page body that was never raw material.
        self.store.store_artifact(scope=scope, artifact=artifact)
        self._record_mapping(
            project_id,
            [
                ("page", slug, "artifact", artifact.artifact_id),
                ("page_source", slug, "source", source_id),
                ("page_revision", slug, "revision", str(revision["revision_id"])),
            ],
        )

    def _mapped_claim_ids(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT new_id FROM legacy_map WHERE knowledge_space_id = ? AND legacy_kind = 'claim' ORDER BY new_id",
            (self.knowledge_space_id,),
        ).fetchall()
        return [str(row["new_id"]) for row in rows]

    def _assert_nothing_auto_verified(self) -> dict[str, Any]:
        """Read the migrated claims back and refuse to report a state a migration cannot grant.

        The extractor is already refused at commit time. This reads the database
        afterwards, so a claim that reached `verified` or `adopted` through any
        other path in this run is caught before the run is called complete.
        """

        claim_ids = self._mapped_claim_ids()
        verified = 0
        adopted = 0
        for start in range(0, len(claim_ids), 400):
            window = claim_ids[start : start + 400]
            placeholders = ",".join("?" * len(window))
            for row in self.connection.execute(
                f"""SELECT v.epistemic_status, v.decision_state
                    FROM claim_versions AS v
                    JOIN claims AS c ON c.claim_id = v.claim_id
                    WHERE v.claim_id IN ({placeholders}) AND v.claim_version_id = c.current_version_id""",
                window,
            ):
                verified += 1 if row["epistemic_status"] == "verified" else 0
                adopted += 1 if row["decision_state"] == "adopted" else 0
        if verified or adopted:
            raise MigrationError(
                f"A migrated claim reads back as verified={verified} adopted={adopted}; "
                "a migration cannot grant either state."
            )
        return {"verified_claims": verified, "adopted_decisions": adopted, "checked": len(claim_ids)}

    def _report(
        self,
        *,
        status: str,
        safety: Mapping[str, Any],
        pending_sources: Sequence[Sequence[str]],
        pending_pages: Sequence[Sequence[str]],
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        source_totals = {"total": 0, "with_raw_text": 0}
        page_totals = {"total": 0, "generated_only": 0, "versions": 0}
        claim_total = 0
        for project in self.projects:
            project_id = str(project["project_id"])
            sources = self.reader.sources(project_id)
            pages = self.reader.pages(project_id)
            with_raw = sum(1 for row in sources if _has_raw_text(row))
            generated_only = 0
            versions = 0
            for page in pages:
                cited, readable = _cited_source_ids(page)
                versions += len(self.reader.history(project_id, str(page["slug"]))["versions"])
                backed = readable and any(
                    (source := self.reader.source(project_id, source_id_value)) is not None
                    and _has_raw_text(source)
                    for source_id_value in cited
                )
                generated_only += 0 if backed else 1
            claims = int(
                self.connection.execute(
                    "SELECT COUNT(*) AS total FROM claims WHERE knowledge_space_id = ? AND project_id = ?",
                    (self.knowledge_space_id, project_id),
                ).fetchone()["total"]
            )
            source_totals["total"] += len(sources)
            source_totals["with_raw_text"] += with_raw
            page_totals["total"] += len(pages)
            page_totals["generated_only"] += generated_only
            page_totals["versions"] += versions
            claim_total += claims
            entries.append(
                {
                    "project_id": project_id,
                    "name": str(project["name"]),
                    "v1_version": int(project["version"]),
                    "knowledge_version": self.store.current_version(project_id),
                    "sources": len(sources),
                    "raw_text_sources": with_raw,
                    "pages": len(pages),
                    "generated_only_pages": generated_only,
                    "versions": versions,
                    "audits": self.reader.audit_count(project_id),
                    "mapped_sources": self._mapping_count(project_id, "source"),
                    "mapped_pages": self._mapping_count(project_id, "page"),
                    "claims": claims,
                }
            )
        return {
            "run_id": self.run_id,
            "knowledge_space_id": self.knowledge_space_id,
            "status": status,
            "complete": status == "completed",
            "checkpoint": self.checkpoint,
            "resume": self.resume,
            "projects": entries,
            "sources": {
                **source_totals,
                "without_raw_text": source_totals["total"] - source_totals["with_raw_text"],
                "mapped": self._mapping_count(None, "source"),
                "artifacts": self._mapping_count(None, "source_artifact"),
            },
            "pages": {
                **page_totals,
                "mapped": self._mapping_count(None, "page"),
                "audits": sum(entry["audits"] for entry in entries),
            },
            "claims": {
                "total": claim_total,
                "extracted": self._mapping_count(None, "claim"),
            },
            "claims_skipped_reason": (
                None if self.extractor is not None else CLAIMS_SKIPPED_WITHOUT_EXTRACTOR
            ),
            "gaps": _v1_gaps(self.reader, self.projects),
            "safety": dict(safety),
            "pending": {
                "sources": [{"project_id": item[0], "source_id": item[1]} for item in pending_sources],
                "pages": [{"project_id": item[0], "slug": item[1]} for item in pending_pages],
            },
        }


def _refuse_self_granted_state(claim: Mapping[str, Any], v1_source_id: str) -> None:
    """A migration is not a verification body and not an adoption record."""

    try:
        state = ClaimState.from_dict(claim["state"])
    except (KeyError, KnowledgeError) as error:
        raise MigrationError(
            f"The extractor returned a claim without a valid state for source {v1_source_id}: {error}"
        ) from error
    if state.epistemic_status == "verified":
        raise MigrationError(
            f"The extractor returned a verified claim for source {v1_source_id}. A migration "
            "cannot verify: it copies material, and only a recorded verification grants that state."
        )
    if state.decision_state == "adopted":
        raise MigrationError(
            f"The extractor returned an adopted decision for source {v1_source_id}. A migration "
            "cannot adopt: the adopted state needs an adoption record this run does not have."
        )


def migrate(
    *,
    v1_database: str | Path,
    v2_database: str | Path,
    knowledge_space_id: str,
    actor_subject: str,
    run_id: str,
    checkpoint: int | None = None,
    extractor: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Copy a v1 Wiki into the v2 knowledge layer, reentrantly and without inventing knowledge.

    `extractor` is `extractor(context) -> list[dict]`. `context` carries `scope`,
    `source_id`, `revision_id`, `artifact`, `chunks` and `project_id`, and the
    returned claim dicts are the ones `build_change_set` consumes. Cite the
    material through `chunk_evidence`, which returns the very record the store
    registers for that chunk; an id this run did not register is refused rather
    than committed as an unresolvable reference.

    With `extractor=None` the sources, revisions and artifacts are still
    migrated, no claims are invented, and the report says so in
    `claims_skipped_reason`.
    """

    if not isinstance(actor_subject, str) or not actor_subject or len(actor_subject) > 240:
        raise MigrationError("A verified actor subject is required.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise MigrationError("A run id is required.")
    if checkpoint is not None and (
        isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 0
    ):
        raise MigrationError("checkpoint must be a non-negative whole number of sources.")
    if extractor is not None and not callable(extractor):
        raise MigrationError("extractor must be callable.")
    space = str(knowledge_space_id or "").strip()
    if not space:
        raise MigrationError("A knowledge space id is required.")
    reader = _V1Reader(_open_v1(v1_database))
    try:
        migration = _Migration(
            reader=reader,
            v2_database=Path(v2_database),
            knowledge_space_id=space,
            actor_subject=actor_subject,
            run_id=run_id,
            checkpoint=checkpoint,
            extractor=extractor,
        )
    except Exception:
        reader.close()
        raise
    try:
        try:
            return migration.run()
        except Exception:
            migration.mark_failed()
            raise
    finally:
        migration.close()
        reader.close()


# ----------------------------------------------------------------------
# Reading the mapping and the migrated text
# ----------------------------------------------------------------------


def _space_for_project(connection: sqlite3.Connection, project_id: str) -> str:
    if not _has_table(connection, "legacy_map"):
        return ""
    rows = connection.execute(
        "SELECT DISTINCT knowledge_space_id FROM legacy_map WHERE project_id = ? ORDER BY knowledge_space_id",
        (project_id,),
    ).fetchall()
    if len(rows) > 1:
        raise MigrationError(
            f"Project {project_id} was migrated into more than one knowledge space; "
            "name the space instead of leaving it ambiguous."
        )
    return str(rows[0]["knowledge_space_id"]) if rows else ""


def _lookup(
    connection: sqlite3.Connection, project_id: str, legacy_kind: str, legacy_id: str
) -> dict[str, Any] | None:
    query = """SELECT knowledge_space_id, project_id, legacy_kind, legacy_id, new_kind, new_id
               FROM legacy_map WHERE project_id = ? AND legacy_kind = ? AND legacy_id = ?
               ORDER BY knowledge_space_id"""
    if not _has_table(connection, "legacy_map"):
        return None
    rows = connection.execute(query, (project_id, legacy_kind, legacy_id)).fetchall()
    if not rows:
        return None
    spaces = {str(row["knowledge_space_id"]) for row in rows}
    if len(spaces) > 1:
        raise MigrationError(
            f"Project {project_id} was migrated into more than one knowledge space; "
            "name the space instead of leaving it ambiguous."
        )
    return dict(rows[0])


def legacy_mapping(v2_database: str | Path, project_id: str) -> dict[str, dict[str, dict[str, str]]]:
    """Every identity this migration created for one project, by legacy kind and id.

    The return value is `{legacy_kind: {legacy_id: {"new_kind", "new_id"}}}`, so a
    caller can answer "what did this v1 object become" without a scan per object.
    """

    connection = _connect(Path(v2_database), read_only=True)
    try:
        if not _has_table(connection, "legacy_map"):
            return {}
        rows = connection.execute(
            """SELECT knowledge_space_id, project_id, legacy_kind, legacy_id, new_kind, new_id
               FROM legacy_map WHERE project_id = ? ORDER BY legacy_kind, legacy_id""",
            (project_id,),
        ).fetchall()
        spaces = {str(row["knowledge_space_id"]) for row in rows}
        if len(spaces) > 1:
            raise MigrationError(
                f"Project {project_id} was migrated into more than one knowledge space; "
                "name the space instead of leaving it ambiguous."
            )
        mapping: dict[str, dict[str, dict[str, str]]] = {}
        for row in rows:
            mapping.setdefault(str(row["legacy_kind"]), {})[str(row["legacy_id"])] = {
                "new_kind": str(row["new_kind"]),
                "new_id": str(row["new_id"]),
            }
        return mapping
    finally:
        connection.close()


def resolve_legacy(
    v2_database: str | Path, project_id: str, legacy_kind: str, legacy_id: str
) -> dict[str, Any] | None:
    """What one v1 object became in v2, or None when it was never migrated."""

    connection = _connect(Path(v2_database), read_only=True)
    try:
        return _lookup(connection, project_id, legacy_kind, legacy_id)
    finally:
        connection.close()


def chunk_evidence(*, project_id: str, artifact: Any, chunk: Any) -> EvidenceRecord:
    """The citation for one chunk of one artifact.

    An extractor calls this during a migration to get the evidence id it must
    cite. The id derives from the project, the artifact and the chunk's spans, so
    the same chunk yields the same id on every run, and the migration registers
    exactly the records whose ids the claims named.
    """

    return make_evidence(
        project_id=project_id,
        artifact=artifact,
        spans=chunk.evidence(),
        heading_path=chunk.heading_path,
        label=chunk.chunk_id,
    )


def legacy_text(
    *,
    v2_database: str | Path,
    knowledge_space_id: str,
    project_id: str,
    legacy_kind: str,
    legacy_id: str,
) -> dict[str, Any]:
    """The migrated text of one v1 object, and whether it can be quoted.

    `legacy_kind` is `"source"` or `"page"`. `stored_text` is the artifact the
    migration froze; it is the stored snapshot, not a quotation. `exact_text` is
    filled only from a registered evidence reference resolved through
    `ClaimStore.load_evidence`, so a `legacy_generated_page` returns none: there
    is no citation for generated text, and inventing one is what this split
    exists to prevent.
    """

    key = {"source": "source_artifact", "page": "page"}.get(legacy_kind)
    if key is None:
        raise MigrationError(f"legacy_kind must be 'source' or 'page'; got {legacy_kind!r}.")
    result: dict[str, Any] = {
        "legacy_kind": legacy_kind,
        "legacy_id": legacy_id,
        "project_id": project_id,
        "knowledge_space_id": knowledge_space_id,
        "found": False,
        "artifact_id": None,
        "source_id": None,
        "parse_quality": None,
        "legacy_unverified": False,
        "raw_available": False,
        "stored_text": None,
        "evidence_ids": [],
        "exact_text": None,
        "quote_error": None,
    }
    connection = _connect(Path(v2_database), read_only=True)
    try:
        if not _has_table(connection, "legacy_map"):
            return result
        entry = _lookup(connection, project_id, key, legacy_id)
        if entry is None:
            return result
        space = str(entry["knowledge_space_id"])
        if space != str(knowledge_space_id):
            raise MigrationError(
                f"{legacy_kind} {legacy_id} was migrated into knowledge space {space}, "
                f"not {knowledge_space_id}."
            )
        artifact = connection.execute(
            """SELECT a.artifact_id, a.normalized_text, a.structure_json, a.parse_quality,
                      r.raw_available
               FROM parsed_artifacts AS a
               JOIN source_revisions AS r ON r.revision_id = a.revision_id
               WHERE a.artifact_id = ? AND a.project_id = ?""",
            (str(entry["new_id"]), project_id),
        ).fetchone()
        if artifact is None:
            return result
        evidence_ids = [
            str(row["evidence_id"])
            for row in connection.execute(
                "SELECT evidence_id FROM evidence_refs WHERE artifact_id = ? ORDER BY evidence_id",
                (str(artifact["artifact_id"]),),
            )
        ]
    finally:
        connection.close()
    structure = json.loads(artifact["structure_json"]) or {}
    result.update(
        {
            "found": True,
            "artifact_id": str(artifact["artifact_id"]),
            "parse_quality": str(artifact["parse_quality"]),
            "legacy_unverified": bool(structure.get("legacy_unverified")),
            "raw_available": bool(artifact["raw_available"]),
            "stored_text": str(artifact["normalized_text"]),
            "evidence_ids": evidence_ids,
        }
    )
    if legacy_kind == "source":
        source_entry = resolve_legacy(v2_database, project_id, "source", legacy_id)
        result["source_id"] = str(source_entry["new_id"]) if source_entry else None
    if evidence_ids:
        store = ClaimStore(v2_database, knowledge_space_id=str(knowledge_space_id))
        scope = Scope.of(str(knowledge_space_id), project_id)
        try:
            recovered = store.load_evidence(evidence_ids[0], scope)
        except (EvidenceError, ClaimStoreError) as error:
            result["quote_error"] = getattr(error, "code", "STORE_ERROR")
        else:
            result["exact_text"] = recovered.exact_text
    return result


# ----------------------------------------------------------------------
# Backup, restore and rollback
# ----------------------------------------------------------------------


def backup_database(*, source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Snapshot a database through SQLite's own backup API, never a file copy.

    The API copies pages of a consistent database, so a snapshot taken while a
    writer is mid-transaction cannot contain half of it, which is exactly what a
    byte-for-byte copy of the file plus its journal can.
    """

    source_path = Path(source)
    if not source_path.is_file():
        raise MigrationError(f"The database to back up does not exist: {source_path}.")
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in _sidecar_paths(destination_path):
        with suppress(OSError):
            stale.unlink()
    with closing(_connect(source_path, read_only=True)) as reader:
        with closing(_connect(destination_path)) as writer:
            reader.backup(writer)
    return {
        "source": str(source_path),
        "destination": str(destination_path),
        "bytes": destination_path.stat().st_size,
        "sha256": _sha256_file(destination_path),
    }


def restore_database(*, backup: str | Path, destination: str | Path, overwrite: bool = False) -> dict[str, Any]:
    """Restore a snapshot to a path, refusing to replace an existing file by default."""

    backup_path = Path(backup)
    if not backup_path.is_file():
        raise MigrationError(f"The backup to restore does not exist: {backup_path}.")
    destination_path = Path(destination)
    existed = destination_path.exists()
    if existed and not overwrite:
        raise MigrationError(
            f"Refusing to overwrite {destination_path}. Pass overwrite=True to replace it."
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in _sidecar_paths(destination_path):
        with suppress(OSError):
            stale.unlink()
    with closing(_connect(backup_path, read_only=True)) as reader:
        with closing(_connect(destination_path)) as writer:
            reader.backup(writer)
    return {
        "backup": str(backup_path),
        "destination": str(destination_path),
        "bytes": destination_path.stat().st_size,
        "sha256": _sha256_file(destination_path),
        "overwritten": existed,
    }


def _sidecar_paths(path: Path) -> tuple[Path, Path, Path]:
    return (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))


def verify_restore(*, database: str | Path, knowledge_space_id: str, project_id: str) -> dict[str, Any]:
    """Read a restored database the way a reader would, and report what resolved.

    `readable` means the v2 schema is present, the project is registered in the
    knowledge space, and every stored citation resolved to exact text. A file
    that merely opens is not a usable restore, so every evidence row is resolved
    through `ClaimStore.load_evidence` and the failures are listed by code.
    """

    path = Path(database)
    counts = {
        "projects": 0,
        "sources": 0,
        "claims": 0,
        "claim_versions": 0,
        "evidence_refs": 0,
        "knowledge_version": 0,
    }
    if not path.is_file():
        return {
            "readable": False,
            "counts": counts,
            "evidence_verified": 0,
            "evidence_failed": 0,
            "failures": [
                {
                    "evidence_id": "",
                    "code": "DATABASE_MISSING",
                    "error": f"{path} is not a file.",
                }
            ],
        }
    connection = _connect(path, read_only=True)
    try:
        if not _has_table(connection, "claims") or not _has_table(connection, "evidence_refs"):
            return {
                "readable": False,
                "counts": counts,
                "evidence_verified": 0,
                "evidence_failed": 0,
                "failures": [
                    {
                        "evidence_id": "",
                        "code": "SCHEMA_MISSING",
                        "error": f"{path} has no v2 knowledge schema.",
                    }
                ],
            }
        counts["projects"] = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_projects WHERE knowledge_space_id = ?",
                (knowledge_space_id,),
            ).fetchone()["total"]
        )
        counts["sources"] = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM sources WHERE knowledge_space_id = ? AND project_id = ?",
                (knowledge_space_id, project_id),
            ).fetchone()["total"]
        )
        counts["claims"] = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM claims WHERE knowledge_space_id = ? AND project_id = ?",
                (knowledge_space_id, project_id),
            ).fetchone()["total"]
        )
        counts["claim_versions"] = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM claim_versions WHERE knowledge_space_id = ? AND project_id = ?",
                (knowledge_space_id, project_id),
            ).fetchone()["total"]
        )
        counts["evidence_refs"] = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM evidence_refs WHERE knowledge_space_id = ? AND project_id = ?",
                (knowledge_space_id, project_id),
            ).fetchone()["total"]
        )
        version = connection.execute(
            "SELECT knowledge_version FROM knowledge_meta WHERE project_id = ? AND knowledge_space_id = ?",
            (project_id, knowledge_space_id),
        ).fetchone()
        counts["knowledge_version"] = int(version["knowledge_version"]) if version else 0
        project_present = bool(
            connection.execute(
                "SELECT 1 FROM knowledge_projects WHERE knowledge_space_id = ? AND project_id = ?",
                (knowledge_space_id, project_id),
            ).fetchone()
        )
        evidence_ids = [
            str(row["evidence_id"])
            for row in connection.execute(
                """SELECT evidence_id FROM evidence_refs
                   WHERE knowledge_space_id = ? AND project_id = ? ORDER BY evidence_id""",
                (knowledge_space_id, project_id),
            )
        ]
        cited_generated = [
            str(row["evidence_id"])
            for row in connection.execute(
                """SELECT e.evidence_id FROM evidence_refs AS e
                   JOIN parsed_artifacts AS a ON a.artifact_id = e.artifact_id
                   WHERE e.knowledge_space_id = ? AND e.project_id = ? AND a.parse_quality = ?
                   ORDER BY e.evidence_id""",
                (knowledge_space_id, project_id, LEGACY_PAGE_PARSE_QUALITY),
            )
        ]
    finally:
        connection.close()
    failures: list[dict[str, Any]] = [
        {
            "evidence_id": evidence_id,
            "code": "EVIDENCE_ON_GENERATED_PAGE",
            "error": "A generated page carries an evidence reference; generated text is not citable.",
        }
        for evidence_id in cited_generated
    ]
    store = ClaimStore(path, knowledge_space_id=str(knowledge_space_id))
    scope = Scope.of(str(knowledge_space_id), project_id)
    verified = 0
    for evidence_id in evidence_ids:
        if evidence_id in cited_generated:
            continue
        try:
            recovered = store.load_evidence(evidence_id, scope)
        except EvidenceError as error:
            failures.append(
                {"evidence_id": evidence_id, "code": error.code, "error": str(error)}
            )
            continue
        except ClaimStoreError as error:
            failures.append(
                {"evidence_id": evidence_id, "code": "STORE_ERROR", "error": str(error)}
            )
            continue
        if not recovered.exact_text:
            failures.append(
                {
                    "evidence_id": evidence_id,
                    "code": "EMPTY_QUOTE",
                    "error": "The citation resolved to no text.",
                }
            )
            continue
        verified += 1
    readable = bool(project_present and not failures)
    return {
        "readable": readable,
        "counts": counts,
        "evidence_verified": verified,
        "evidence_failed": len(failures),
        "failures": failures,
    }


def rollback_plan(*, v2_database: str | Path, backup: str | Path) -> dict[str, Any]:
    """What undoing a cutover would cost, counted rather than promised.

    The plan compares the live database with the backup row by row and names the
    writes the backup does not have. When there are none, restoring the backup is
    the whole rollback. When there are, restoring it drops them, and the plan says
    so: an older snapshot is a state, not a history, and nothing here can put a
    claim written after it back into it.
    """

    live_path = Path(v2_database)
    backup_path = Path(backup)
    if not live_path.is_file():
        raise MigrationError(f"The database does not exist: {live_path}.")
    if not backup_path.is_file():
        raise MigrationError(f"The backup does not exist: {backup_path}.")
    new_writes: dict[str, list[str]] = {}
    with closing(_connect(live_path, read_only=True)) as live:
        with closing(_connect(backup_path, read_only=True)) as snapshot:
            for table, column in _WRITE_TABLES:
                if not _has_table(live, table):
                    continue
                live_ids = {
                    str(row[0]) for row in live.execute(f"SELECT {column} FROM {table}")
                }
                snapshot_ids = (
                    {str(row[0]) for row in snapshot.execute(f"SELECT {column} FROM {table}")}
                    if _has_table(snapshot, table)
                    else set()
                )
                added = sorted(live_ids - snapshot_ids)
                if added:
                    new_writes[table] = added
    total = sum(len(ids) for ids in new_writes.values())
    identifier = sorted({identifier for ids in new_writes.values() for identifier in ids})
    if total:
        options = ["read_only_fallback", "replay_from_log"]
        statement = (
            f"Restoring this backup drops the {total} v2 write(s) recorded after it, together "
            "with their claims, decisions and citations. Those writes exist only in the live "
            "database. Bring them back by replaying the ingest log into the restored database, "
            "or keep serving the live database read-only until they are replayed."
        )
    else:
        options = ["restore_backup"]
        statement = (
            "No v2 write happened after this backup, so restoring it returns the database to "
            "the state the backup records and drops nothing."
        )
    return {
        "v2_database": str(live_path),
        "backup": str(backup_path),
        "before_cutover": (
            "Before cutover the v1 database is the record of what was there and the v2 database "
            "is a copy built from it. A backup taken at this point is the rollback boundary: if "
            "the v2 database has not been written since that backup, restoring it returns to the "
            "recorded state exactly."
        ),
        "after_cutover": (
            "After cutover v2 is where knowledge is written and the v1 database is frozen. "
            "Every write made after the backup exists only in the live database, so restoring "
            "the backup drops them. Restoring an older snapshot is not a way to take writes "
            "back: it leaves the state the snapshot records, and the writes made after it have "
            "to be replayed from the log or the live database served read-only while they are."
        ),
        "v2_writes_since_backup": total,
        "new_write_ids": identifier,
        "new_writes": new_writes,
        "options": options,
        "restore_backup_drops_writes": bool(total),
        "statement": statement,
    }
