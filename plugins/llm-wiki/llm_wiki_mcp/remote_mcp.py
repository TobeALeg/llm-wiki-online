"""Authenticated Streamable HTTP MCP surface for the shared Wiki."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .auth import AuthService


def _allowed_hosts(issuer_url: str) -> list[str]:
    """Hosts FastMCP may accept, since it runs behind a reverse proxy.

    FastMCP enables DNS-rebinding protection when it is constructed with a
    loopback host, which only permits loopback `Host` values. The shared Wiki is
    reached through nginx under its public hostname, so that hostname has to be
    allowed explicitly or every remote MCP request fails with 421.
    """

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    parsed = urllib.parse.urlsplit(issuer_url)
    if parsed.hostname:
        netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        hosts.append(netloc)
    return hosts


class SubjectBoundTokenVerifier:
    """Translate lw's opaque token into MCP auth info without trusting arguments."""

    def __init__(self, auth: AuthService):
        self.auth = auth

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            member = self.auth.authenticate_mcp_token(token)
        except Exception:
            return None
        return AccessToken(
            token=token,
            client_id=member["subject"],
            scopes=["wiki:read", "wiki:write"],
        )


def context_subject() -> str:
    """Return the subject attached by MCP's bearer middleware."""

    access = get_access_token()
    if access is None or not access.client_id:
        raise PermissionError("Authentication required.")
    return access.client_id


def context_token() -> str:
    access = get_access_token()
    if access is None or not access.token:
        raise PermissionError("Authentication required.")
    return access.token


def create_remote_mcp(
    auth: AuthService,
    read_status: Callable[[str], dict[str, Any]],
    *,
    issuer_url: str = "https://lw.app.mentti.work",
    read_search: Callable[[str, str, int], dict[str, Any]] | None = None,
    read_page: Callable[[str, str], dict[str, Any]] | None = None,
    read_versions: Callable[[str, str], dict[str, Any]] | None = None,
    submit_update: Callable[..., dict[str, Any]] | None = None,
    restore_page: Callable[..., dict[str, Any]] | None = None,
    organize_local: Callable[..., dict[str, Any]] | None = None,
    revoke_token: Callable[[str], None] | None = None,
) -> FastMCP:
    """Create the protected `/mcp` server and register its first read seam."""

    issuer = AnyHttpUrl(issuer_url.rstrip("/"))
    resource = AnyHttpUrl(issuer_url.rstrip("/") + "/mcp")
    server = FastMCP(
        "llm-wiki-remote",
        instructions=(
            "The caller is identified by its verified mentti-bound credential. "
            "Never accept subject, email, or name as an identity override."
        ),
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource,
            required_scopes=["wiki:read", "wiki:write"],
        ),
        token_verifier=SubjectBoundTokenVerifier(auth),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_hosts(issuer_url),
        ),
    )

    @server.tool(
        name="company_wiki_status",
        title="Read shared Wiki status",
        description="Read the committed shared Wiki status for the authenticated member.",
    )
    def company_wiki_status(ctx: Context) -> dict[str, Any]:
        return read_status(context_subject())

    if read_search:
        @server.tool(
            name="company_wiki_search",
            title="Search shared Wiki",
            description="Search committed shared Wiki pages for the authenticated member.",
        )
        def company_wiki_search(query: str, limit: int = 20, ctx: Context = None) -> dict[str, Any]:
            return read_search(context_subject(), query, limit)

    if read_page:
        @server.tool(
            name="company_wiki_page",
            title="Read shared Wiki page",
            description="Read one committed shared Wiki page by slug.",
        )
        def company_wiki_page(slug: str, ctx: Context = None) -> dict[str, Any]:
            return read_page(context_subject(), slug)

    if read_versions:
        @server.tool(
            name="company_wiki_versions",
            title="List shared Wiki history",
            description="List immutable versions of one shared Wiki page.",
        )
        def company_wiki_versions(slug: str, ctx: Context = None) -> dict[str, Any]:
            return read_versions(context_subject(), slug)

    if submit_update:
        @server.tool(
            name="company_wiki_submit",
            title="Submit shared Wiki update",
            description="Organize selected materials and atomically submit them to the shared Wiki.",
        )
        def company_wiki_submit(base_version: int, idempotency_key: str, materials: list[dict[str, Any]], purpose: str, ctx: Context = None) -> dict[str, Any]:
            return submit_update(context_subject(), base_version, idempotency_key, materials, purpose)

    if restore_page:
        @server.tool(
            name="company_wiki_restore",
            title="Restore shared Wiki page",
            description="Restore a historical page version as a new committed version.",
        )
        def company_wiki_restore(slug: str, version_id: int, base_version: int, idempotency_key: str, ctx: Context = None) -> dict[str, Any]:
            return restore_page(context_subject(), slug, version_id, base_version, idempotency_key)

    if organize_local:
        @server.tool(
            name="local_wiki_organize",
            title="Organize selected local Wiki material",
            description="Return a validated Wiki update package without persisting the selected material or result on this service.",
        )
        def local_wiki_organize(materials: list[dict[str, Any]], existing_pages: list[dict[str, Any]], purpose: str, ctx: Context = None) -> dict[str, Any]:
            context_subject()
            return organize_local(materials, existing_pages, purpose)

    if revoke_token:
        @server.tool(
            name="company_wiki_revoke_credential",
            title="Revoke current Wiki credential",
            description="Immediately revoke the bearer credential used for this call.",
        )
        def company_wiki_revoke_credential(ctx: Context = None) -> dict[str, Any]:
            revoke_token(context_token())
            return {"status": "revoked"}

    return server
