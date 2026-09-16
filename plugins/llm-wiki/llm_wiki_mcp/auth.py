"""Menti identity exchange and short-lived lw credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Protocol


class AuthError(RuntimeError):
    """An authentication failure that is safe to return to a caller."""


class IdentityProvider(Protocol):
    def exchange_code(self, code: str) -> dict[str, Any]: ...


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_text(value: Any, field: str, maximum: int = 240) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise AuthError(f"Menti identity field {field} is invalid.")
    return result


class AuthStore:
    """Small durable auth store containing no passwords or Menti session data."""

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
                CREATE TABLE IF NOT EXISTS members (
                    subject TEXT PRIMARY KEY,
                    email TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_tokens (
                    token_hash TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    processed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
                """
            )

    def register_authorization_code(self, code: str, ttl_seconds: int = 300) -> None:
        if not code or len(code) > 512:
            raise AuthError("Authorization code is invalid.")
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO authorization_codes(code_hash, expires_at, used_at) VALUES (?, ?, NULL)",
                (_hash(code), _now() + max(1, ttl_seconds)),
            )

    def consume_authorization_code(self, code: str) -> None:
        code_hash = _hash(code)
        current = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires_at, used_at FROM authorization_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if row and (row["used_at"] is not None or row["expires_at"] < current):
                raise AuthError("Authorization code is expired or has already been used.")
            if row:
                db.execute(
                    "UPDATE authorization_codes SET used_at = ? WHERE code_hash = ?",
                    (current, code_hash),
                )
            else:
                db.execute(
                    "INSERT INTO authorization_codes(code_hash, expires_at, used_at) VALUES (?, ?, ?)",
                    (code_hash, current + 1, current),
                )
            db.commit()

    def issue_state(self, ttl_seconds: int = 300) -> str:
        state = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute(
                "INSERT INTO oauth_states(state_hash, expires_at) VALUES (?, ?)",
                (_hash(state), _now() + max(1, ttl_seconds)),
            )
        return state

    def consume_state(self, state: str) -> None:
        state_hash = _hash(state)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires_at FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            if not row or row["expires_at"] < _now():
                raise AuthError("OAuth state is invalid or expired.")
            db.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
            db.commit()

    def upsert_member(self, identity: dict[str, Any], *, sequence: int | None = None) -> dict[str, Any]:
        subject = _required_text(identity.get("subject", identity.get("sub")), "subject")
        email = str(identity.get("email", "") or "").strip()[:320]
        name = str(identity.get("name", "") or "").strip()[:240]
        enabled = bool(identity.get("enabled", identity.get("active", True)))
        current = _now()
        with self._db() as db:
            row = db.execute("SELECT sequence FROM members WHERE subject = ?", (subject,)).fetchone()
            old_sequence = int(row["sequence"]) if row else 0
            new_sequence = max(old_sequence, int(sequence or 0))
            db.execute(
                """
                INSERT INTO members(subject, email, name, enabled, sequence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject) DO UPDATE SET
                    email = excluded.email,
                    name = excluded.name,
                    enabled = excluded.enabled,
                    sequence = excluded.sequence,
                    updated_at = excluded.updated_at
                """,
                (subject, email, name, int(enabled), new_sequence, current),
            )
        return self.member(subject) or {}

    def member(self, subject: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM members WHERE subject = ?", (subject,)).fetchone()
        if not row:
            return None
        return {"subject": row["subject"], "email": row["email"], "name": row["name"], "enabled": bool(row["enabled"]), "sequence": row["sequence"]}

    def issue_session(self, subject: str, ttl_seconds: int) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        expires = _now() + max(1, ttl_seconds)
        with self._db() as db:
            db.execute("INSERT INTO sessions(token_hash, subject, expires_at) VALUES (?, ?, ?)", (_hash(token), subject, expires))
        return token, expires

    def issue_mcp_token(self, subject: str, ttl_seconds: int) -> tuple[str, int]:
        token = secrets.token_urlsafe(40)
        expires = _now() + max(1, ttl_seconds)
        with self._db() as db:
            db.execute("INSERT INTO mcp_tokens(token_hash, subject, expires_at, revoked_at) VALUES (?, ?, ?, NULL)", (_hash(token), subject, expires))
        return token, expires

    def _subject_for_token(self, table: str, token: str) -> str | None:
        if table not in {"sessions", "mcp_tokens"}:
            raise AuthError("Invalid token table.")
        with self._db() as db:
            if table == "sessions":
                row = db.execute("SELECT subject, expires_at FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
            else:
                row = db.execute("SELECT subject, expires_at, revoked_at FROM mcp_tokens WHERE token_hash = ?", (_hash(token),)).fetchone()
        if not row or row["expires_at"] < _now() or (table == "mcp_tokens" and row["revoked_at"] is not None):
            return None
        return str(row["subject"])

    def subject_for_session(self, token: str) -> str | None:
        return self._subject_for_token("sessions", token)

    def subject_for_mcp_token(self, token: str) -> str | None:
        return self._subject_for_token("mcp_tokens", token)

    def revoke_mcp_token(self, token: str) -> None:
        with self._db() as db:
            db.execute("UPDATE mcp_tokens SET revoked_at = ? WHERE token_hash = ?", (_now(), _hash(token)))

    def apply_event(self, event_id: str, identity: dict[str, Any], sequence: int) -> str:
        event_id = _required_text(event_id, "event_id", 160)
        if not isinstance(sequence, int) or sequence < 0:
            raise AuthError("Member event sequence must be a non-negative integer.")
        subject = _required_text(identity.get("subject", identity.get("sub")), "subject")
        enabled = bool(identity.get("enabled", identity.get("active", True)))
        current = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone():
                db.rollback()
                return "duplicate"
            member = db.execute("SELECT sequence FROM members WHERE subject = ?", (subject,)).fetchone()
            old_sequence = int(member["sequence"]) if member else -1
            db.execute(
                "INSERT INTO webhook_events(event_id, subject, sequence, enabled, processed_at) VALUES (?, ?, ?, ?, ?)",
                (event_id, subject, sequence, int(enabled), current),
            )
            if sequence <= old_sequence:
                db.commit()
                return "stale"
            db.execute(
                """
                INSERT INTO members(subject, email, name, enabled, sequence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject) DO UPDATE SET enabled = excluded.enabled, sequence = excluded.sequence, updated_at = excluded.updated_at
                """,
                (subject, str(identity.get("email", "") or "")[:320], str(identity.get("name", "") or "")[:240], int(enabled), sequence, current),
            )
            db.commit()
        return "applied"

    def reconcile(self, identities: Iterable[dict[str, Any]]) -> int:
        identities = list(identities)
        normalized = []
        seen_subjects = set()
        for identity in identities:
            if not isinstance(identity, dict):
                raise AuthError("Each reconciled member must be an object.")
            subject = _required_text(identity.get("subject", identity.get("sub")), "subject")
            if subject in seen_subjects:
                raise AuthError(f"Member directory contains duplicate subject {subject}.")
            seen_subjects.add(subject)
            normalized.append({
                "subject": subject,
                "email": str(identity.get("email", "") or "").strip()[:320],
                "name": str(identity.get("name", "") or "").strip()[:240],
                "enabled": bool(identity.get("enabled", identity.get("active", True))),
            })
        changed = 0
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current_rows = {row["subject"]: row for row in db.execute("SELECT * FROM members")}
            now = _now()
            for identity in normalized:
                current = current_rows.get(identity["subject"])
                if current and bool(current["enabled"]) == identity["enabled"] and current["email"] == identity["email"] and current["name"] == identity["name"]:
                    continue
                sequence = int(current["sequence"]) + 1 if current else 0
                db.execute(
                    """
                    INSERT INTO members(subject, email, name, enabled, sequence, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subject) DO UPDATE SET email=excluded.email, name=excluded.name,
                        enabled=excluded.enabled, sequence=excluded.sequence, updated_at=excluded.updated_at
                    """,
                    (identity["subject"], identity["email"], identity["name"], int(identity["enabled"]), sequence, now),
                )
                changed += 1
            rows = db.execute("SELECT subject, sequence FROM members WHERE enabled = 1").fetchall()
            for row in rows:
                if row["subject"] in seen_subjects:
                    continue
                db.execute(
                    "UPDATE members SET enabled = 0, sequence = ?, updated_at = ? WHERE subject = ?",
                    (int(row["sequence"]) + 1, now, row["subject"]),
                )
                changed += 1
            db.commit()
        return changed


class MentiIdentityProvider:
    """Adapter for Menti's existing authorization-code exchange endpoint."""

    def __init__(self, exchange_url: str | None = None):
        self.exchange_url = exchange_url or os.environ.get("MENTI_AUTH_CODE_URL", "").strip()

    def exchange_code(self, code: str) -> dict[str, Any]:
        if not self.exchange_url:
            raise AuthError("MENTI_AUTH_CODE_URL is not configured.")
        client_id = os.environ.get("MENTI_CLIENT_ID", "")
        client_secret = os.environ.get("MENTI_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise AuthError("Menti application credentials are not configured.")
        # Menti's exchange endpoint parses the request body as JSON, not form data.
        payload = json.dumps({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": os.environ.get("MENTI_REDIRECT_URI", ""),
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.exchange_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                value = json.loads(response.read(64 * 1024).decode("utf-8"))
        except Exception as exc:  # provider details must not be exposed to clients
            raise AuthError("Menti identity exchange failed.") from exc
        if not isinstance(value, dict):
            raise AuthError("Menti identity response is invalid.")
        nested = value.get("member") if isinstance(value.get("member"), dict) else value.get("user") if isinstance(value.get("user"), dict) else value
        identity = {
            "subject": nested.get("subject", nested.get("sub")),
            "email": nested.get("email", ""),
            "name": nested.get("name", nested.get("display_name", "")),
            "enabled": nested.get("enabled", nested.get("active", True)),
        }
        _required_text(identity["subject"], "subject")
        return identity

    def list_members(self) -> list[dict[str, Any]]:
        """Fetch the authoritative member list through the configured Menti app endpoint."""

        endpoint = os.environ.get("MENTI_MEMBERS_URL", "").strip()
        if not endpoint:
            raise AuthError("MENTI_MEMBERS_URL is not configured.")
        client_id = os.environ.get("MENTI_CLIENT_ID", "")
        client_secret = os.environ.get("MENTI_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise AuthError("Menti application credentials are not configured.")
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(endpoint, headers={"Authorization": f"Basic {credentials}"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                value = json.loads(response.read(256 * 1024).decode("utf-8"))
        except Exception as exc:
            raise AuthError("Menti member reconciliation failed.") from exc
        members = value.get("members") if isinstance(value, dict) else value
        if not isinstance(members, list):
            raise AuthError("Menti member reconciliation response is invalid.")
        return [{
            "subject": item.get("subject", item.get("sub")),
            "email": item.get("email", ""),
            "name": item.get("name", item.get("display_name", "")),
            "enabled": item.get("enabled", item.get("active", True)),
        } for item in members if isinstance(item, dict)]


class AuthService:
    def __init__(self, store: AuthStore, provider: IdentityProvider | None = None, *, session_ttl: int = 8 * 3600, mcp_ttl: int = 30 * 24 * 3600):
        self.store = store
        self.provider = provider or MentiIdentityProvider()
        self.session_ttl = session_ttl
        self.mcp_ttl = mcp_ttl

    def login_with_code(self, code: str) -> dict[str, Any]:
        if not code or len(code) > 512:
            raise AuthError("Authorization code is invalid.")
        identity = self.provider.exchange_code(code)
        self.store.consume_authorization_code(code)
        subject = _required_text(identity.get("subject", identity.get("sub")), "subject")
        existing = self.store.member(subject)
        if existing and not existing["enabled"]:
            raise AuthError("Menti member is disabled.")
        member = self.store.upsert_member(identity)
        if not member.get("enabled"):
            raise AuthError("Menti member is disabled.")
        token, expires = self.store.issue_session(member["subject"], self.session_ttl)
        return {"session_token": token, "expires_at": expires, "member": member}

    def authenticate_session(self, token: str) -> dict[str, Any]:
        subject = self.store.subject_for_session(token) if token else None
        member = self.store.member(subject) if subject else None
        if not member or not member["enabled"]:
            raise AuthError("Session is invalid or expired.")
        return member

    def issue_mcp_token(self, session_token: str) -> dict[str, Any]:
        member = self.authenticate_session(session_token)
        token, expires = self.store.issue_mcp_token(member["subject"], self.mcp_ttl)
        return {"access_token": token, "token_type": "Bearer", "expires_at": expires, "subject": member["subject"]}

    def authenticate_mcp_token(self, token: str) -> dict[str, Any]:
        subject = self.store.subject_for_mcp_token(token) if token else None
        member = self.store.member(subject) if subject else None
        if not member or not member["enabled"]:
            raise AuthError("MCP credential is invalid, expired, revoked, or disabled.")
        return member

    def revoke_mcp_token(self, token: str) -> None:
        self.store.revoke_mcp_token(token)

    @staticmethod
    def verify_webhook(secret: str, body: bytes, signature: str, timestamp: str = "") -> bool:
        if not secret or not signature:
            return False
        if signature.startswith("sha256="):
            expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        # Menti signs `"{timestamp}.{body}"` and prefixes the digest with `v1=`.
        if signature.startswith("v1=") and timestamp:
            signed = f"{timestamp}.".encode("utf-8") + body
            expected = "v1=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        return False

    def apply_member_webhook(self, event_id: str, identity: dict[str, Any], sequence: int) -> str:
        return self.store.apply_event(event_id, identity, sequence)

    def reconcile_members(self, identities: Iterable[dict[str, Any]]) -> int:
        return self.store.reconcile(identities)
