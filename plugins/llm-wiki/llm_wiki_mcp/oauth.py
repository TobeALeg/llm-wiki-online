"""OAuth 2.1 authorization-server adapter for the protected company MCP."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .auth import AuthError, AuthService


SCOPES = ("wiki:read", "wiki:write")


class OAuthError(RuntimeError):
    """A standards-facing OAuth failure safe to return to a client."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(prefix: str, bytes_count: int = 32) -> str:
    return prefix + secrets.token_urlsafe(bytes_count)


def _text(value: Any, name: str, maximum: int = 2048) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise OAuthError("invalid_request", f"{name} is invalid.")
    return result


def _safe_redirect_uri(value: str) -> str:
    uri = _text(value, "redirect_uri")
    parsed = urllib.parse.urlsplit(uri)
    is_loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.fragment or (parsed.scheme != "https" and not is_loopback):
        raise OAuthError("invalid_redirect_uri", "redirect_uri must use HTTPS or a loopback HTTP address.")
    return uri


class OAuthStore:
    """Durable OAuth state behind the OAuthService interface."""

    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
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
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
                    pending_hash TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    state TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL
                );
                """
            )

    def register_client(self, client_name: str, redirect_uris: list[str]) -> str:
        client_id = "lw_client_" + secrets.token_urlsafe(18)
        with self._db() as db:
            db.execute(
                "INSERT INTO oauth_clients(client_id, client_name, redirect_uris, created_at) VALUES (?, ?, ?, ?)",
                (client_id, client_name, json.dumps(redirect_uris), _now()),
            )
        return client_id

    def client(self, client_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if not row:
            return None
        return {"client_id": row["client_id"], "client_name": row["client_name"], "redirect_uris": json.loads(row["redirect_uris"])}

    def create_pending(self, subject: str, request: dict[str, str], ttl_seconds: int = 300) -> str:
        pending = _token("pending_", 24)
        with self._db() as db:
            db.execute(
                """INSERT INTO oauth_pending_authorizations(
                       pending_hash, subject, client_id, redirect_uri, scope, state,
                       code_challenge, resource, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _hash(pending), subject, request["client_id"], request["redirect_uri"],
                    request["scope"], request["state"], request["code_challenge"],
                    request["resource"], _now() + ttl_seconds,
                ),
            )
        return pending

    def consume_pending(self, pending: str, subject: str) -> dict[str, str]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth_pending_authorizations WHERE pending_hash = ?",
                (_hash(pending),),
            ).fetchone()
            if not row or row["subject"] != subject or row["expires_at"] < _now():
                db.rollback()
                raise OAuthError("invalid_request", "Authorization request is invalid or expired.")
            db.execute("DELETE FROM oauth_pending_authorizations WHERE pending_hash = ?", (_hash(pending),))
            db.commit()
        return dict(row)

    def issue_code(self, request: dict[str, str], ttl_seconds: int = 300) -> str:
        code = _token("lw_code_", 32)
        with self._db() as db:
            db.execute(
                """INSERT INTO oauth_authorization_codes(
                       code_hash, subject, client_id, redirect_uri, scope,
                       code_challenge, resource, expires_at, used_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    _hash(code), request["subject"], request["client_id"], request["redirect_uri"],
                    request["scope"], request["code_challenge"], request["resource"],
                    _now() + ttl_seconds,
                ),
            )
        return code

    def consume_code(self, code: str, client_id: str, redirect_uri: str, code_challenge: str) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth_authorization_codes WHERE code_hash = ?", (_hash(code),)
            ).fetchone()
            if (
                not row or row["used_at"] is not None or row["expires_at"] < _now()
                or row["client_id"] != client_id or row["redirect_uri"] != redirect_uri
                or row["code_challenge"] != code_challenge
            ):
                db.rollback()
                raise OAuthError("invalid_grant", "Authorization code is invalid or expired.")
            db.execute(
                "UPDATE oauth_authorization_codes SET used_at = ? WHERE code_hash = ?",
                (_now(), _hash(code)),
            )
            db.commit()
        return dict(row)

    def issue_refresh(self, subject: str, client_id: str, scope: str, resource: str, ttl_seconds: int) -> str:
        token = _token("lw_refresh_", 40)
        with self._db() as db:
            db.execute(
                """INSERT INTO oauth_refresh_tokens(
                       token_hash, subject, client_id, scope, resource,
                       expires_at, revoked_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                (_hash(token), subject, client_id, scope, resource, _now() + ttl_seconds, _now()),
            )
        return token

    def rotate_refresh(self, token: str, client_id: str, ttl_seconds: int) -> tuple[dict[str, Any], str]:
        replacement = _token("lw_refresh_", 40)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth_refresh_tokens WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
            if (
                not row or row["revoked_at"] is not None or row["expires_at"] < _now()
                or row["client_id"] != client_id
            ):
                db.rollback()
                raise OAuthError("invalid_grant", "Refresh token is invalid or expired.")
            db.execute(
                "UPDATE oauth_refresh_tokens SET revoked_at = ? WHERE token_hash = ?",
                (_now(), _hash(token)),
            )
            db.execute(
                """INSERT INTO oauth_refresh_tokens(
                       token_hash, subject, client_id, scope, resource,
                       expires_at, revoked_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    _hash(replacement), row["subject"], row["client_id"], row["scope"],
                    row["resource"], _now() + ttl_seconds, _now(),
                ),
            )
            db.commit()
        return dict(row), replacement

    def revoke_refresh(self, token: str, client_id: str) -> None:
        with self._db() as db:
            db.execute(
                """UPDATE oauth_refresh_tokens SET revoked_at = ?
                   WHERE token_hash = ? AND client_id = ? AND revoked_at IS NULL""",
                (_now(), _hash(token), client_id),
            )


class OAuthService:
    """Small interface hiding OAuth discovery, PKCE, codes, and refresh rotation."""

    def __init__(
        self,
        auth: AuthService,
        *,
        issuer: str = "https://lw.app.mentti.work",
        access_ttl: int = 3600,
        refresh_ttl: int = 30 * 24 * 3600,
    ):
        self.auth = auth
        self.store = OAuthStore(auth.store.database)
        self.issuer = issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(SCOPES),
            "resource_documentation": self.issuer + "/readme.md",
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + "/oauth/authorize",
            "token_endpoint": self.issuer + "/oauth/token",
            "registration_endpoint": self.issuer + "/oauth/register",
            "revocation_endpoint": self.issuer + "/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES),
        }

    def register_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        uris = payload.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 10:
            raise OAuthError("invalid_client_metadata", "redirect_uris must be a non-empty list.")
        redirects = [_safe_redirect_uri(str(uri)) for uri in uris]
        if len(set(redirects)) != len(redirects):
            raise OAuthError("invalid_client_metadata", "redirect_uris must be unique.")
        client_name = str(payload.get("client_name") or "MCP client").strip()[:120]
        client_id = self.store.register_client(client_name, redirects)
        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirects,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    def begin_authorization(self, params: dict[str, str], subject: str) -> dict[str, str]:
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
        client_id = _text(params.get("client_id"), "client_id", 240)
        client = self.store.client(client_id)
        if not client:
            raise OAuthError("invalid_request", "client_id is not registered.")
        redirect_uri = _safe_redirect_uri(params.get("redirect_uri", ""))
        if redirect_uri not in client["redirect_uris"]:
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client.")
        if params.get("code_challenge_method") != "S256":
            raise OAuthError("invalid_request", "PKCE code_challenge_method must be S256.")
        challenge = _text(params.get("code_challenge"), "code_challenge", 128)
        resource = _text(params.get("resource"), "resource")
        if resource != self.resource:
            raise OAuthError("invalid_target", "resource must identify this MCP endpoint.")
        requested = [item for item in params.get("scope", "wiki:read wiki:write").split() if item]
        if set(requested) != set(SCOPES):
            raise OAuthError("invalid_scope", "This server currently requires wiki:read and wiki:write together.")
        request = {
            "client_id": client_id,
            "client_name": client["client_name"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(dict.fromkeys(requested)),
            "state": _text(params.get("state"), "state"),
            "code_challenge": challenge,
            "resource": resource,
        }
        request["pending_id"] = self.store.create_pending(subject, request)
        return request

    def finish_authorization(self, pending_id: str, subject: str, approved: bool) -> str:
        request = self.store.consume_pending(pending_id, subject)
        query: dict[str, str]
        if approved:
            code = self.store.issue_code(request)
            query = {"code": code, "state": request["state"]}
        else:
            query = {"error": "access_denied", "state": request["state"]}
        separator = "&" if urllib.parse.urlsplit(request["redirect_uri"]).query else "?"
        return request["redirect_uri"] + separator + urllib.parse.urlencode(query)

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def exchange_token(self, form: dict[str, str]) -> dict[str, Any]:
        grant_type = form.get("grant_type")
        client_id = _text(form.get("client_id"), "client_id", 240)
        if form.get("resource") != self.resource:
            raise OAuthError("invalid_target", "resource must identify this MCP endpoint.")
        if grant_type == "authorization_code":
            code = _text(form.get("code"), "code", 512)
            redirect_uri = _safe_redirect_uri(form.get("redirect_uri", ""))
            verifier = _text(form.get("code_verifier"), "code_verifier", 128)
            if not 43 <= len(verifier) <= 128 or not all(char.isalnum() or char in "-._~" for char in verifier):
                raise OAuthError("invalid_grant", "PKCE code_verifier is invalid.")
            request = self.store.consume_code(code, client_id, redirect_uri, self._pkce_challenge(verifier))
            subject, scope, resource = request["subject"], request["scope"], request["resource"]
            refresh = self.store.issue_refresh(subject, client_id, scope, resource, self.refresh_ttl)
        elif grant_type == "refresh_token":
            request, refresh = self.store.rotate_refresh(
                _text(form.get("refresh_token"), "refresh_token", 512), client_id, self.refresh_ttl
            )
            subject, scope, resource = request["subject"], request["scope"], request["resource"]
        else:
            raise OAuthError("unsupported_grant_type", "Only authorization_code and refresh_token are supported.")
        try:
            access = self.auth.issue_mcp_token_for_subject(
                subject, self.access_ttl, label="OAuth client", token_kind="oauth"
            )
        except AuthError as exc:
            raise OAuthError("invalid_grant", "The member is no longer authorized.") from exc
        return {
            "access_token": access["access_token"],
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "refresh_token": refresh,
            "scope": scope,
            "resource": resource,
        }

    def revoke_token(self, form: dict[str, str]) -> None:
        client_id = _text(form.get("client_id"), "client_id", 240)
        if not self.store.client(client_id):
            raise OAuthError("invalid_client", "client_id is not registered.", 401)
        token = _text(form.get("token"), "token", 512)
        if token.startswith("lw_refresh_"):
            self.store.revoke_refresh(token, client_id)
        else:
            self.auth.revoke_mcp_token(token)
