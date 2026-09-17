"""Atomic, project-scoped storage for the company Wiki."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .core import CoreError, MAX_EXISTING_PAGES, normalize_materials, validate_update_package


DEFAULT_PROJECT_ID = "company"
PROJECT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
PAGE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class StoreError(RuntimeError):
    """A storage or request consistency failure."""


class ConflictError(StoreError):
    def __init__(self, message: str, *, current_version: int):
        super().__init__(message)
        self.current_version = current_version


class IdempotencyError(StoreError):
    pass


class PageNotFoundError(StoreError):
    pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _request_hash(project_id: str, base_version: int, materials: list[dict[str, str]], update: dict[str, Any], purpose: str = "") -> str:
    material_fingerprints = [
        {"source_id": item["source_id"], "kind": item["kind"], "label": item["label"], "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()}
        for item in materials
    ]
    canonical_update = dict(update)
    canonical_update["pages"] = [{key: value for key, value in page.items() if key != "updated_at"} for page in update["pages"]]
    # updated_at is assigned while validating a package, so it is not part of the request identity.
    payload = {"project_id": project_id, "base_version": base_version, "materials": material_fingerprints, "purpose": purpose, "update": canonical_update}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _intent_hash(project_id: str, base_version: int, materials: list[dict[str, str]], purpose: str = "") -> str:
    material_fingerprints = [
        {"source_id": item["source_id"], "kind": item["kind"], "label": item["label"], "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()}
        for item in materials
    ]
    payload = {"project_id": project_id, "base_version": base_version, "materials": material_fingerprints, "purpose": purpose}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class SharedWikiStore:
    """Durable project-scoped company Wiki storage; callers never provide a filesystem root."""

    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
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

    def initialize(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS wiki_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO wiki_meta(key, value) VALUES ('current_version', '0');
                CREATE TABLE IF NOT EXISTS wiki_projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            if self._legacy_schema(db):
                self._migrate_legacy_schema(db)
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS wiki_project_meta (
                    project_id TEXT PRIMARY KEY REFERENCES wiki_projects(id) ON DELETE CASCADE,
                    current_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS wiki_sources (
                    project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE,
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY (project_id, slug)
                );
                CREATE TABLE IF NOT EXISTS wiki_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    actor_subject TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    previous_version INTEGER
                );
                CREATE INDEX IF NOT EXISTS wiki_versions_project_slug ON wiki_versions(project_id, slug, version DESC);
                CREATE TABLE IF NOT EXISTS wiki_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    before_version INTEGER NOT NULL,
                    after_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wiki_submissions (
                    project_id TEXT NOT NULL REFERENCES wiki_projects(id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    intent_hash TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, idempotency_key)
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO wiki_projects(id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
                (DEFAULT_PROJECT_ID, "Company Wiki", "system", _now_iso()),
            )
            legacy_version = int(db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()["value"])
            db.execute(
                "INSERT OR IGNORE INTO wiki_project_meta(project_id, current_version) VALUES (?, ?)",
                (DEFAULT_PROJECT_ID, legacy_version),
            )
            self._add_missing_columns(db)

    @staticmethod
    def _add_missing_columns(db: sqlite3.Connection) -> None:
        """Add columns a database created by an earlier version does not have yet.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new column only
        reaches an old database through an explicit ALTER. The default keeps existing rows
        readable as pages without aliases.
        """

        wanted = {"wiki_pages": ("aliases_json",), "wiki_versions": ("aliases_json",)}
        for table, columns in wanted.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'")

    @staticmethod
    def _legacy_schema(db: sqlite3.Connection) -> bool:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_pages'").fetchone()
        if not row:
            return False
        return "project_id" not in {column["name"] for column in db.execute("PRAGMA table_info(wiki_pages)")}

    @staticmethod
    def _migrate_legacy_schema(db: sqlite3.Connection) -> None:
        """Move the original single-scope schema into the default company project."""
        for table in ("wiki_sources", "wiki_pages", "wiki_versions", "wiki_audits", "wiki_submissions"):
            db.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        db.execute("DROP INDEX IF EXISTS wiki_versions_slug")

        db.executescript(
            """
            CREATE TABLE wiki_project_meta (project_id TEXT PRIMARY KEY, current_version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE wiki_sources (
                project_id TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
                content TEXT NOT NULL, content_sha256 TEXT NOT NULL, actor_subject TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (project_id, source_id)
            );
            CREATE TABLE wiki_pages (
                project_id TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
                tags_json TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL, source_ids_json TEXT NOT NULL,
                updated_at TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY (project_id, slug)
            );
            CREATE TABLE wiki_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, version INTEGER NOT NULL, slug TEXT NOT NULL,
                title TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL, summary TEXT NOT NULL,
                body TEXT NOT NULL, source_ids_json TEXT NOT NULL, actor_subject TEXT NOT NULL, action TEXT NOT NULL,
                created_at TEXT NOT NULL, previous_version INTEGER
            );
            CREATE INDEX wiki_versions_project_slug ON wiki_versions(project_id, slug, version DESC);
            CREATE TABLE wiki_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, version INTEGER NOT NULL, action TEXT NOT NULL,
                actor_subject TEXT NOT NULL, summary TEXT NOT NULL, source_ids_json TEXT NOT NULL,
                before_version INTEGER NOT NULL, after_version INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE wiki_submissions (
                project_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, intent_hash TEXT,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (project_id, idempotency_key)
            );
            """
        )
        db.execute("INSERT INTO wiki_project_meta(project_id, current_version) SELECT ?, CAST(value AS INTEGER) FROM wiki_meta WHERE key='current_version'", (DEFAULT_PROJECT_ID,))
        db.execute("INSERT INTO wiki_sources SELECT ?, source_id, kind, label, content, content_sha256, actor_subject, created_at FROM wiki_sources_legacy", (DEFAULT_PROJECT_ID,))
        db.execute("INSERT INTO wiki_pages SELECT ?, slug, title, type, status, tags_json, summary, body, source_ids_json, updated_at, version FROM wiki_pages_legacy", (DEFAULT_PROJECT_ID,))
        db.execute("INSERT INTO wiki_versions SELECT id, ?, version, slug, title, type, status, tags_json, summary, body, source_ids_json, actor_subject, action, created_at, previous_version FROM wiki_versions_legacy", (DEFAULT_PROJECT_ID,))
        db.execute("INSERT INTO wiki_audits SELECT id, ?, version, action, actor_subject, summary, source_ids_json, before_version, after_version, created_at FROM wiki_audits_legacy", (DEFAULT_PROJECT_ID,))
        db.execute("INSERT INTO wiki_submissions SELECT ?, idempotency_key, request_hash, intent_hash, result_json, created_at FROM wiki_submissions_legacy", (DEFAULT_PROJECT_ID,))
        for table in ("wiki_sources", "wiki_pages", "wiki_versions", "wiki_audits", "wiki_submissions"):
            db.execute(f"DROP TABLE {table}_legacy")

    @staticmethod
    def _normalize_project_id(project_id: str) -> str:
        normalized = str(project_id or "").strip().lower()
        if not PROJECT_ID.fullmatch(normalized):
            raise StoreError("Project ID must be 1-64 lowercase letters, digits, underscores, or hyphens.")
        return normalized

    def _ensure_project(self, db: sqlite3.Connection, project_id: str) -> str:
        normalized = self._normalize_project_id(project_id)
        if not db.execute("SELECT 1 FROM wiki_projects WHERE id = ?", (normalized,)).fetchone():
            raise StoreError(f"Unknown Wiki project: {normalized}")
        return normalized

    def create_project(self, project_id: str, name: str, actor_subject: str) -> dict[str, Any]:
        normalized = self._normalize_project_id(project_id)
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > 120:
            raise StoreError("Project name must be 1-120 characters.")
        if not actor_subject or len(actor_subject) > 240:
            raise StoreError("A verified actor subject is required.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                created_at = _now_iso()
                db.execute("INSERT INTO wiki_projects(id, name, created_by, created_at) VALUES (?, ?, ?, ?)", (normalized, clean_name, actor_subject, created_at))
                db.execute("INSERT INTO wiki_project_meta(project_id, current_version) VALUES (?, 0)", (normalized,))
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise StoreError(f"Wiki project already exists: {normalized}") from exc
        return {"id": normalized, "name": clean_name, "version": 0, "page_count": 0}

    def list_projects(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT p.id, p.name, p.created_at, m.current_version,
                          (SELECT COUNT(*) FROM wiki_pages w WHERE w.project_id = p.id) AS page_count
                   FROM wiki_projects p JOIN wiki_project_meta m ON m.project_id = p.id
                   ORDER BY lower(p.name), p.id"""
            ).fetchall()
        return [{"id": row["id"], "name": row["name"], "created_at": row["created_at"], "version": row["current_version"], "page_count": row["page_count"]} for row in rows]

    def current_version(self, project_id: str = DEFAULT_PROJECT_ID) -> int:
        with self._db() as db:
            normalized = self._ensure_project(db, project_id)
            row = db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized,)).fetchone()
        return int(row["current_version"] if row else 0)

    @staticmethod
    def _page(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "project_id": row["project_id"],
            "slug": row["slug"],
            "title": row["title"],
            "type": row["type"],
            "status": row["status"],
            "tags": json.loads(row["tags_json"]),
            "summary": row["summary"],
            "body": row["body"],
            "sources": json.loads(row["source_ids_json"]),
            "aliases": json.loads(row["aliases_json"]),
            "updated_at": row["updated_at"],
            "version": row["version"],
        }

    def _known_source_ids(self, db: sqlite3.Connection, project_id: str) -> set[str]:
        return {row["source_id"] for row in db.execute("SELECT source_id FROM wiki_sources WHERE project_id = ?", (project_id,))}

    def list_pages(self, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN")
            try:
                normalized = self._ensure_project(db, project_id)
                version = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized,)).fetchone()["current_version"])
                rows = db.execute("SELECT * FROM wiki_pages WHERE project_id = ? ORDER BY lower(title), slug", (normalized,)).fetchall()
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"version": version, "pages": [self._page(row) for row in rows]}

    def list_page_catalog(self, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        """Identity for every page, without any page body.

        This is what the routing phase reads. Sending the whole snapshot to choose affected
        pages costs the same as the snapshot itself, so the catalog exists to keep that
        decision off the page text.
        """

        with self._db() as db:
            db.execute("BEGIN")
            try:
                normalized = self._ensure_project(db, project_id)
                version = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized,)).fetchone()["current_version"])
                rows = db.execute(
                    "SELECT slug, title, type, status, summary, aliases_json FROM wiki_pages WHERE project_id = ? ORDER BY lower(title), slug",
                    (normalized,),
                ).fetchall()
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "version": version,
            "entries": [{
                "slug": row["slug"],
                "title": row["title"],
                "type": row["type"],
                "status": row["status"],
                "summary": row["summary"],
                "aliases": json.loads(row["aliases_json"]),
            } for row in rows],
        }

    def get_pages_by_slugs(self, slugs: Iterable[str], project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        """Full pages for exactly the named slugs, preserving the caller's order.

        Unknown slugs are reported rather than dropped, so a caller that routed to a page
        which vanished cannot silently merge against a shorter set.
        """

        wanted: list[str] = []
        for slug in slugs:
            if not isinstance(slug, str) or not PAGE_SLUG.fullmatch(slug):
                raise StoreError(f"Invalid page slug: {slug!r}")
            if slug not in wanted:
                wanted.append(slug)
        if len(wanted) > MAX_EXISTING_PAGES:
            raise StoreError(f"Too many page slugs requested; the limit is {MAX_EXISTING_PAGES}.")
        with self._db() as db:
            db.execute("BEGIN")
            try:
                normalized = self._ensure_project(db, project_id)
                version = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized,)).fetchone()["current_version"])
                found: dict[str, sqlite3.Row] = {}
                for start in range(0, len(wanted), 400):
                    window = wanted[start:start + 400]
                    placeholders = ",".join("?" * len(window))
                    rows = db.execute(
                        f"SELECT * FROM wiki_pages WHERE project_id = ? AND slug IN ({placeholders})",
                        (normalized, *window),
                    ).fetchall()
                    found.update({row["slug"]: row for row in rows})
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "version": version,
            "pages": [self._page(found[slug]) for slug in wanted if slug in found],
            "missing": [slug for slug in wanted if slug not in found],
        }

    def search_pages(self, query: str, limit: int = 20, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        query = str(query or "").strip().lower()
        if len(query) > 200:
            raise StoreError("Search query exceeds 200 characters.")
        tokens = [token for token in query.split() if token]
        with self._db() as db:
            db.execute("BEGIN")
            try:
                normalized = self._ensure_project(db, project_id)
                version = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized,)).fetchone()["current_version"])
                rows = db.execute("SELECT * FROM wiki_pages WHERE project_id = ?", (normalized,)).fetchall()
                db.commit()
            except Exception:
                db.rollback()
                raise
        ranked = []
        for row in rows:
            text = " ".join((row["title"], row["summary"], row["body"], row["tags_json"])).lower()
            score = sum(text.count(token) for token in tokens) if tokens else 1
            if score:
                ranked.append((score, self._page(row)))
        ranked.sort(key=lambda item: (-item[0], item[1]["title"].lower(), item[1]["slug"]))
        return {"version": version, "pages": [page for _, page in ranked[: max(1, min(limit, 50))]]}

    def get_page(self, slug: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any] | None:
        if not isinstance(slug, str) or not PAGE_SLUG.fullmatch(slug):
            raise StoreError("Invalid page slug.")
        with self._db() as db:
            db.execute("BEGIN")
            try:
                normalized = self._ensure_project(db, project_id)
                version = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized,)).fetchone()["current_version"])
                row = db.execute("SELECT * FROM wiki_pages WHERE project_id = ? AND slug = ?", (normalized, slug)).fetchone()
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"version": version, "page": self._page(row) if row else None}

    def intent_hash(self, base_version: int, materials: Iterable[Any], purpose: str = "", project_id: str = DEFAULT_PROJECT_ID) -> str:
        if not isinstance(base_version, int) or base_version < 0:
            raise StoreError("base_version must be a non-negative integer.")
        try:
            normalized_materials = normalize_materials(materials)
        except CoreError as exc:
            raise StoreError(str(exc)) from exc
        purpose = str(purpose or "").strip()
        if len(purpose) > 8_000:
            raise StoreError("purpose exceeds the 8000 character limit.")
        return _intent_hash(self._normalize_project_id(project_id), base_version, normalized_materials, purpose)

    def submission(self, idempotency_key: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any] | None:
        if not isinstance(idempotency_key, str) or not __import__("re").fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", idempotency_key):
            raise StoreError("idempotency_key must be a short stable identifier.")
        with self._db() as db:
            normalized = self._ensure_project(db, project_id)
            row = db.execute("SELECT request_hash, intent_hash, result_json FROM wiki_submissions WHERE project_id = ? AND idempotency_key = ?", (normalized, idempotency_key)).fetchone()
        if not row:
            return None
        return {"request_hash": row["request_hash"], "intent_hash": row["intent_hash"], "result": json.loads(row["result_json"])}

    def commit_update(
        self,
        actor_subject: str,
        base_version: int,
        idempotency_key: str,
        materials: Iterable[Any],
        update: dict[str, Any],
        *,
        purpose: str = "",
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> dict[str, Any]:
        if not actor_subject or len(actor_subject) > 240:
            raise StoreError("A verified actor subject is required.")
        if not isinstance(base_version, int) or base_version < 0:
            raise StoreError("base_version must be a non-negative integer.")
        if not isinstance(idempotency_key, str) or not __import__("re").fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", idempotency_key):
            raise StoreError("idempotency_key must be a short stable identifier.")
        try:
            normalized_materials = normalize_materials(materials)
        except CoreError as exc:
            raise StoreError(str(exc)) from exc
        purpose = str(purpose or "").strip()
        if len(purpose) > 8_000:
            raise StoreError("purpose exceeds the 8000 character limit.")
        normalized_project_id = self._normalize_project_id(project_id)
        with self._db() as db:
            self._ensure_project(db, normalized_project_id)
            allowed_sources = self._known_source_ids(db, normalized_project_id) | {item["source_id"] for item in normalized_materials}
            try:
                normalized_update = validate_update_package(update, allowed_sources)
            except CoreError as exc:
                raise StoreError(str(exc)) from exc
            request_hash = _request_hash(normalized_project_id, base_version, normalized_materials, normalized_update, purpose)
            intent_hash = _intent_hash(normalized_project_id, base_version, normalized_materials, purpose)
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT request_hash, result_json FROM wiki_submissions WHERE project_id = ? AND idempotency_key = ?", (normalized_project_id, idempotency_key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    db.rollback()
                    raise IdempotencyError("Idempotency key was already used for a different request.")
                db.commit()
                return json.loads(existing["result_json"])
            current = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized_project_id,)).fetchone()["current_version"])
            if base_version != current:
                db.rollback()
                raise ConflictError(f"Wiki changed since base_version {base_version}; retry from version {current}.", current_version=current)
            new_version = current + 1
            created_at = _now_iso()
            for material in normalized_materials:
                digest = hashlib.sha256(material["content"].encode("utf-8")).hexdigest()
                old = db.execute("SELECT content_sha256 FROM wiki_sources WHERE project_id = ? AND source_id = ?", (normalized_project_id, material["source_id"])).fetchone()
                if old and old["content_sha256"] != digest:
                    db.rollback()
                    raise StoreError(f"Source {material['source_id']} is immutable and cannot change.")
                if not old:
                    db.execute(
                        "INSERT INTO wiki_sources(project_id, source_id, kind, label, content, content_sha256, actor_subject, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (normalized_project_id, material["source_id"], material["kind"], material["label"], material["content"], digest, actor_subject, created_at),
                    )
            changed = []
            for page in normalized_update["pages"]:
                old = db.execute("SELECT version FROM wiki_pages WHERE project_id = ? AND slug = ?", (normalized_project_id, page["slug"])).fetchone()
                old_version = int(old["version"]) if old else None
                db.execute(
                    """
                    INSERT INTO wiki_versions(project_id, version, slug, title, type, status, tags_json, summary, body, source_ids_json, aliases_json, actor_subject, action, created_at, previous_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (normalized_project_id, new_version, page["slug"], page["title"], page["type"], page["status"], _json(page["tags"]), page["summary"], page["body"], _json(page["sources"]), _json(page["aliases"]), actor_subject, "update", created_at, old_version),
                )
                db.execute(
                    """
                    INSERT INTO wiki_pages(project_id, slug, title, type, status, tags_json, summary, body, source_ids_json, aliases_json, updated_at, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, slug) DO UPDATE SET title=excluded.title, type=excluded.type, status=excluded.status, tags_json=excluded.tags_json, summary=excluded.summary, body=excluded.body, source_ids_json=excluded.source_ids_json, aliases_json=excluded.aliases_json, updated_at=excluded.updated_at, version=excluded.version
                    """,
                    (normalized_project_id, page["slug"], page["title"], page["type"], page["status"], _json(page["tags"]), page["summary"], page["body"], _json(page["sources"]), _json(page["aliases"]), created_at, new_version),
                )
                changed.append(page["slug"])
            summary = str(normalized_update.get("note", "Wiki updated."))
            source_ids = normalized_update.get("source_ids", [])
            db.execute("INSERT INTO wiki_audits(project_id, version, action, actor_subject, summary, source_ids_json, before_version, after_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (normalized_project_id, new_version, "update", actor_subject, summary, _json(source_ids), current, new_version, created_at))
            db.execute("UPDATE wiki_project_meta SET current_version = ? WHERE project_id = ?", (new_version, normalized_project_id))
            result = {"project_id": normalized_project_id, "version": new_version, "changed_pages": changed, "idempotency_key": idempotency_key}
            db.execute("INSERT INTO wiki_submissions(project_id, idempotency_key, request_hash, intent_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (normalized_project_id, idempotency_key, request_hash, intent_hash, _json(result), created_at))
            db.commit()
            return result

    def page_versions(self, slug: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        if not isinstance(slug, str) or not PAGE_SLUG.fullmatch(slug):
            raise StoreError("Invalid page slug.")
        with self._db() as db:
            db.execute("BEGIN")
            try:
                normalized = self._ensure_project(db, project_id)
                current = db.execute("SELECT version FROM wiki_pages WHERE project_id = ? AND slug = ?", (normalized, slug)).fetchone()
                if not current:
                    raise PageNotFoundError("Wiki page does not exist.")
                current_version = int(current["version"])
                rows = db.execute(
                    """
                    SELECT v.id, v.version, v.slug, v.title, v.type, v.status, v.tags_json,
                           v.summary, v.body, v.source_ids_json, v.actor_subject, v.action,
                           v.created_at, v.previous_version, a.summary AS audit_summary,
                           a.before_version, a.after_version
                    FROM wiki_versions AS v
                    LEFT JOIN wiki_audits AS a ON a.project_id = v.project_id AND a.version = v.version AND a.action = v.action
                    WHERE v.project_id = ? AND v.slug = ? ORDER BY v.version DESC, v.id DESC
                    """,
                    (normalized, slug),
                ).fetchall()
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "project_id": normalized,
            "slug": slug,
            "versions": [{
                "id": row["id"], "version": row["version"], "title": row["title"], "type": row["type"], "status": row["status"] if row["version"] == current_version else ("superseded" if row["status"] == "current" else row["status"]), "tags": json.loads(row["tags_json"]), "summary": row["summary"], "body": row["body"], "audit_summary": row["audit_summary"] or "", "sources": json.loads(row["source_ids_json"]), "actor_subject": row["actor_subject"], "action": row["action"], "created_at": row["created_at"], "previous_version": row["previous_version"], "before_version": row["before_version"], "after_version": row["after_version"],
            } for row in rows],
        }

    def restore_page(self, actor_subject: str, slug: str, version_id: int, base_version: int, idempotency_key: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        if not isinstance(slug, str) or not PAGE_SLUG.fullmatch(slug):
            raise StoreError("Invalid page slug.")
        if not isinstance(version_id, int) or version_id < 1:
            raise StoreError("version_id must be a positive integer.")
        if not isinstance(idempotency_key, str) or not __import__("re").fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", idempotency_key):
            raise StoreError("idempotency_key must be a short stable identifier.")
        normalized_project_id = self._normalize_project_id(project_id)
        request_hash = hashlib.sha256(_json({"project_id": normalized_project_id, "slug": slug, "version_id": version_id, "base_version": base_version}).encode("utf-8")).hexdigest()
        with self._db() as db:
            self._ensure_project(db, normalized_project_id)
            db.execute("BEGIN IMMEDIATE")
            old_submission = db.execute("SELECT request_hash, result_json FROM wiki_submissions WHERE project_id = ? AND idempotency_key = ?", (normalized_project_id, idempotency_key)).fetchone()
            if old_submission:
                if old_submission["request_hash"] != request_hash:
                    db.rollback()
                    raise IdempotencyError("Idempotency key was already used for a different request.")
                db.commit()
                return json.loads(old_submission["result_json"])
            current = int(db.execute("SELECT current_version FROM wiki_project_meta WHERE project_id = ?", (normalized_project_id,)).fetchone()["current_version"])
            if base_version != current:
                db.rollback()
                raise ConflictError(f"Wiki changed since base_version {base_version}; retry from version {current}.", current_version=current)
            historical = db.execute("SELECT * FROM wiki_versions WHERE id = ? AND project_id = ? AND slug = ?", (version_id, normalized_project_id, slug)).fetchone()
            if not historical:
                db.rollback()
                raise StoreError("Historical page version does not exist.")
            current_page = db.execute("SELECT version FROM wiki_pages WHERE project_id = ? AND slug = ?", (normalized_project_id, slug)).fetchone()
            new_version = current + 1
            created_at = _now_iso()
            db.execute(
                "INSERT INTO wiki_versions(project_id, version, slug, title, type, status, tags_json, summary, body, source_ids_json, aliases_json, actor_subject, action, created_at, previous_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (normalized_project_id, new_version, slug, historical["title"], historical["type"], historical["status"], historical["tags_json"], historical["summary"], historical["body"], historical["source_ids_json"], historical["aliases_json"], actor_subject, "restore", created_at, current_page["version"] if current_page else None),
            )
            db.execute(
                "UPDATE wiki_pages SET title=?, type=?, status=?, tags_json=?, summary=?, body=?, source_ids_json=?, aliases_json=?, updated_at=?, version=? WHERE project_id=? AND slug=?",
                (historical["title"], historical["type"], historical["status"], historical["tags_json"], historical["summary"], historical["body"], historical["source_ids_json"], historical["aliases_json"], created_at, new_version, normalized_project_id, slug),
            )
            summary = f"Restored page {slug} from version {version_id}."
            db.execute("INSERT INTO wiki_audits(project_id, version, action, actor_subject, summary, source_ids_json, before_version, after_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (normalized_project_id, new_version, "restore", actor_subject, summary, historical["source_ids_json"], current, new_version, created_at))
            db.execute("UPDATE wiki_project_meta SET current_version=? WHERE project_id=?", (new_version, normalized_project_id))
            result = {"project_id": normalized_project_id, "version": new_version, "changed_pages": [slug], "restored_version_id": version_id, "idempotency_key": idempotency_key}
            db.execute("INSERT INTO wiki_submissions(project_id, idempotency_key, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?)", (normalized_project_id, idempotency_key, request_hash, _json(result), created_at))
            db.commit()
            return result

    def audit_log(self, limit: int = 100, project_id: str = DEFAULT_PROJECT_ID) -> list[dict[str, Any]]:
        with self._db() as db:
            normalized = self._ensure_project(db, project_id)
            rows = db.execute("SELECT * FROM wiki_audits WHERE project_id = ? ORDER BY id DESC LIMIT ?", (normalized, max(1, min(limit, 500)))).fetchall()
        return [{
            "project_id": row["project_id"],
            "version": row["version"], "action": row["action"], "actor_subject": row["actor_subject"], "summary": row["summary"], "sources": json.loads(row["source_ids_json"]), "before_version": row["before_version"], "after_version": row["after_version"], "created_at": row["created_at"],
        } for row in rows]
