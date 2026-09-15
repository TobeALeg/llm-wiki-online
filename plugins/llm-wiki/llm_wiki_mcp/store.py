"""Atomic, single-scope storage for the company Wiki."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .core import CoreError, normalize_materials, validate_update_package


class StoreError(RuntimeError):
    """A storage or request consistency failure."""


class ConflictError(StoreError):
    def __init__(self, message: str, *, current_version: int):
        super().__init__(message)
        self.current_version = current_version


class IdempotencyError(StoreError):
    pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _request_hash(base_version: int, materials: list[dict[str, str]], update: dict[str, Any]) -> str:
    material_fingerprints = [
        {"source_id": item["source_id"], "kind": item["kind"], "label": item["label"], "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()}
        for item in materials
    ]
    payload = {"base_version": base_version, "materials": material_fingerprints, "update": update}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class SharedWikiStore:
    """The only durable company-Wiki scope; callers never provide a filesystem root."""

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
                CREATE TABLE IF NOT EXISTS wiki_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO wiki_meta(key, value) VALUES ('current_version', '0');
                CREATE TABLE IF NOT EXISTS wiki_sources (
                    source_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wiki_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    previous_version INTEGER
                );
                CREATE INDEX IF NOT EXISTS wiki_versions_slug ON wiki_versions(slug, version DESC);
                CREATE TABLE IF NOT EXISTS wiki_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def current_version(self) -> int:
        with self._db() as db:
            row = db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()
        return int(row["value"] if row else 0)

    @staticmethod
    def _page(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "slug": row["slug"],
            "title": row["title"],
            "type": row["type"],
            "status": row["status"],
            "tags": json.loads(row["tags_json"]),
            "summary": row["summary"],
            "body": row["body"],
            "sources": json.loads(row["source_ids_json"]),
            "updated_at": row["updated_at"],
            "version": row["version"],
        }

    def _known_source_ids(self, db: sqlite3.Connection) -> set[str]:
        return {row["source_id"] for row in db.execute("SELECT source_id FROM wiki_sources")}

    def list_pages(self) -> dict[str, Any]:
        with self._db() as db:
            version = int(db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()["value"])
            rows = db.execute("SELECT * FROM wiki_pages ORDER BY lower(title), slug").fetchall()
        return {"version": version, "pages": [self._page(row) for row in rows]}

    def search_pages(self, query: str, limit: int = 20) -> dict[str, Any]:
        query = str(query or "").strip().lower()
        if len(query) > 200:
            raise StoreError("Search query exceeds 200 characters.")
        tokens = [token for token in query.split() if token]
        with self._db() as db:
            version = int(db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()["value"])
            rows = db.execute("SELECT * FROM wiki_pages").fetchall()
        ranked = []
        for row in rows:
            text = " ".join((row["title"], row["summary"], row["body"], row["tags_json"])).lower()
            score = sum(text.count(token) for token in tokens) if tokens else 1
            if score:
                ranked.append((score, self._page(row)))
        ranked.sort(key=lambda item: (-item[0], item[1]["title"].lower(), item[1]["slug"]))
        return {"version": version, "pages": [page for _, page in ranked[: max(1, min(limit, 50))]]}

    def get_page(self, slug: str) -> dict[str, Any] | None:
        if not isinstance(slug, str) or not __import__("re").fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise StoreError("Invalid page slug.")
        with self._db() as db:
            version = int(db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()["value"])
            row = db.execute("SELECT * FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
        return {"version": version, "page": self._page(row) if row else None}

    def commit_update(
        self,
        actor_subject: str,
        base_version: int,
        idempotency_key: str,
        materials: Iterable[Any],
        update: dict[str, Any],
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
        with self._db() as db:
            allowed_sources = self._known_source_ids(db) | {item["source_id"] for item in normalized_materials}
            try:
                normalized_update = validate_update_package(update, allowed_sources)
            except CoreError as exc:
                raise StoreError(str(exc)) from exc
            request_hash = _request_hash(base_version, normalized_materials, normalized_update)
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT request_hash, result_json FROM wiki_submissions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    db.rollback()
                    raise IdempotencyError("Idempotency key was already used for a different request.")
                db.commit()
                return json.loads(existing["result_json"])
            current = int(db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()["value"])
            if base_version != current:
                db.rollback()
                raise ConflictError(f"Wiki changed since base_version {base_version}; retry from version {current}.", current_version=current)
            new_version = current + 1
            created_at = _now_iso()
            for material in normalized_materials:
                digest = hashlib.sha256(material["content"].encode("utf-8")).hexdigest()
                old = db.execute("SELECT content_sha256 FROM wiki_sources WHERE source_id = ?", (material["source_id"],)).fetchone()
                if old and old["content_sha256"] != digest:
                    db.rollback()
                    raise StoreError(f"Source {material['source_id']} is immutable and cannot change.")
                if not old:
                    db.execute(
                        "INSERT INTO wiki_sources(source_id, kind, label, content, content_sha256, actor_subject, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (material["source_id"], material["kind"], material["label"], material["content"], digest, actor_subject, created_at),
                    )
            changed = []
            for page in normalized_update["pages"]:
                old = db.execute("SELECT version FROM wiki_pages WHERE slug = ?", (page["slug"],)).fetchone()
                old_version = int(old["version"]) if old else None
                db.execute(
                    """
                    INSERT INTO wiki_versions(version, slug, title, type, status, tags_json, summary, body, source_ids_json, actor_subject, action, created_at, previous_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (new_version, page["slug"], page["title"], page["type"], page["status"], _json(page["tags"]), page["summary"], page["body"], _json(page["sources"]), actor_subject, "update", created_at, old_version),
                )
                db.execute(
                    """
                    INSERT INTO wiki_pages(slug, title, type, status, tags_json, summary, body, source_ids_json, updated_at, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET title=excluded.title, type=excluded.type, status=excluded.status, tags_json=excluded.tags_json, summary=excluded.summary, body=excluded.body, source_ids_json=excluded.source_ids_json, updated_at=excluded.updated_at, version=excluded.version
                    """,
                    (page["slug"], page["title"], page["type"], page["status"], _json(page["tags"]), page["summary"], page["body"], _json(page["sources"]), created_at, new_version),
                )
                changed.append(page["slug"])
            summary = str(normalized_update.get("note", "Wiki updated."))
            source_ids = normalized_update.get("source_ids", [])
            db.execute("INSERT INTO wiki_audits(version, action, actor_subject, summary, source_ids_json, before_version, after_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (new_version, "update", actor_subject, summary, _json(source_ids), current, new_version, created_at))
            db.execute("UPDATE wiki_meta SET value = ? WHERE key = 'current_version'", (str(new_version),))
            result = {"version": new_version, "changed_pages": changed, "idempotency_key": idempotency_key}
            db.execute("INSERT INTO wiki_submissions(idempotency_key, request_hash, result_json, created_at) VALUES (?, ?, ?, ?)", (idempotency_key, request_hash, _json(result), created_at))
            db.commit()
            return result

    def page_versions(self, slug: str) -> dict[str, Any]:
        self.get_page(slug)
        with self._db() as db:
            current = db.execute("SELECT version FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
            current_version = int(current["version"]) if current else None
            rows = db.execute("SELECT id, version, slug, title, type, status, tags_json, summary, source_ids_json, actor_subject, action, created_at, previous_version FROM wiki_versions WHERE slug = ? ORDER BY version DESC, id DESC", (slug,)).fetchall()
        return {
            "slug": slug,
            "versions": [{
                "id": row["id"], "version": row["version"], "title": row["title"], "type": row["type"], "status": row["status"] if row["version"] == current_version else ("superseded" if row["status"] == "current" else row["status"]), "tags": json.loads(row["tags_json"]), "summary": row["summary"], "sources": json.loads(row["source_ids_json"]), "actor_subject": row["actor_subject"], "action": row["action"], "created_at": row["created_at"], "previous_version": row["previous_version"],
            } for row in rows],
        }

    def restore_page(self, actor_subject: str, slug: str, version_id: int, base_version: int, idempotency_key: str) -> dict[str, Any]:
        if not isinstance(version_id, int) or version_id < 1:
            raise StoreError("version_id must be a positive integer.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = int(db.execute("SELECT value FROM wiki_meta WHERE key = 'current_version'").fetchone()["value"])
            if base_version != current:
                db.rollback()
                raise ConflictError(f"Wiki changed since base_version {base_version}; retry from version {current}.", current_version=current)
            old_submission = db.execute("SELECT result_json FROM wiki_submissions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if old_submission:
                db.commit()
                return json.loads(old_submission["result_json"])
            historical = db.execute("SELECT * FROM wiki_versions WHERE id = ? AND slug = ?", (version_id, slug)).fetchone()
            if not historical:
                db.rollback()
                raise StoreError("Historical page version does not exist.")
            current_page = db.execute("SELECT version FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
            new_version = current + 1
            created_at = _now_iso()
            db.execute(
                "INSERT INTO wiki_versions(version, slug, title, type, status, tags_json, summary, body, source_ids_json, actor_subject, action, created_at, previous_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_version, slug, historical["title"], historical["type"], historical["status"], historical["tags_json"], historical["summary"], historical["body"], historical["source_ids_json"], actor_subject, "restore", created_at, current_page["version"] if current_page else None),
            )
            db.execute(
                "UPDATE wiki_pages SET title=?, type=?, status=?, tags_json=?, summary=?, body=?, source_ids_json=?, updated_at=?, version=? WHERE slug=?",
                (historical["title"], historical["type"], historical["status"], historical["tags_json"], historical["summary"], historical["body"], historical["source_ids_json"], created_at, new_version, slug),
            )
            summary = f"Restored page {slug} from version {version_id}."
            db.execute("INSERT INTO wiki_audits(version, action, actor_subject, summary, source_ids_json, before_version, after_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (new_version, "restore", actor_subject, summary, historical["source_ids_json"], current, new_version, created_at))
            db.execute("UPDATE wiki_meta SET value=? WHERE key='current_version'", (str(new_version),))
            result = {"version": new_version, "changed_pages": [slug], "restored_version_id": version_id, "idempotency_key": idempotency_key}
            request_hash = hashlib.sha256(_json({"slug": slug, "version_id": version_id, "base_version": base_version}).encode("utf-8")).hexdigest()
            db.execute("INSERT INTO wiki_submissions(idempotency_key, request_hash, result_json, created_at) VALUES (?, ?, ?, ?)", (idempotency_key, request_hash, _json(result), created_at))
            db.commit()
            return result

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM wiki_audits ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [{
            "version": row["version"], "action": row["action"], "actor_subject": row["actor_subject"], "summary": row["summary"], "sources": json.loads(row["source_ids_json"]), "before_version": row["before_version"], "after_version": row["after_version"], "created_at": row["created_at"],
        } for row in rows]
