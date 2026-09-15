"""Authenticated browser API and minimal static reader for the shared Wiki."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .auth import AuthError, AuthService
from .model import ModelError
from .remote_service import RemoteWikiService
from .shared_service import SharedWikiService
from .store import ConflictError, StoreError


class NotFoundError(RuntimeError):
    pass


READER_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LLM Wiki</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #f6f5f1; color: #20221f; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; }
    header { display: flex; gap: 24px; align-items: center; padding: 22px max(24px, calc((100vw - 1180px) / 2)); background: #1f332b; color: #f9f7ef; }
    header h1 { margin: 0; font: 600 24px/1.1 Georgia, serif; letter-spacing: .02em; }
    form { display: flex; flex: 1; max-width: 620px; gap: 8px; }
    input { width: 100%; border: 1px solid #c9c9bd; border-radius: 999px; padding: 10px 16px; font: inherit; background: #fffef9; }
    button { border: 0; border-radius: 999px; padding: 10px 18px; background: #d4a94a; color: #1d241e; font-weight: 650; cursor: pointer; }
    main { display: grid; grid-template-columns: minmax(260px, 360px) 1fr; gap: 22px; max-width: 1180px; margin: 30px auto; padding: 0 24px; }
    aside, article { background: #fffef9; border: 1px solid #deddd3; border-radius: 14px; }
    aside { padding: 16px; }
    aside h2 { margin: 0 0 12px; font: 600 18px Georgia, serif; }
    .page-list { display: grid; gap: 8px; }
    .page-card { display: block; text-align: left; width: 100%; border-radius: 10px; background: transparent; padding: 12px; border: 1px solid transparent; }
    .page-card:hover, .page-card.active { border-color: #b9c7b9; background: #eff4ed; }
    .page-card strong { display: block; margin-bottom: 4px; }
    .page-card span { color: #666b64; font-size: 13px; }
    article { padding: clamp(22px, 5vw, 52px); min-height: 560px; }
    article h2 { margin-top: 0; font: 600 clamp(28px, 4vw, 44px)/1.08 Georgia, serif; }
    .meta { color: #62675f; font-size: 14px; display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 28px; }
    .body { max-width: 760px; line-height: 1.75; }
    .body h3 { font: 600 22px/1.2 Georgia, serif; margin: 26px 0 8px; }
    .body p { margin: 12px 0; }
    .body code { background: #efeee7; padding: 2px 5px; border-radius: 4px; }
    .body a { color: #275d48; }
    .sources { border-top: 1px solid #deddd3; margin-top: 34px; padding-top: 16px; color: #62675f; font-size: 13px; }
    .state { color: #62675f; padding: 22px 8px; line-height: 1.6; }
    @media (max-width: 760px) { header { flex-wrap: wrap; } form { order: 2; flex-basis: 100%; } main { grid-template-columns: 1fr; margin-top: 18px; } article { min-height: 420px; } }
  </style>
</head>
<body>
  <header><h1>LLM Wiki</h1><form id="search"><input name="q" aria-label="搜索 Wiki" autocomplete="off"><button>搜索</button></form></header>
  <main><aside><h2>页面目录</h2><div id="list" class="page-list"><div class="state">正在读取…</div></div></aside><article id="detail"><div class="state">请选择一个页面。</div></article></main>
  <script>
    const list = document.getElementById('list');
    const detail = document.getElementById('detail');
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const safeMarkdown = (value) => {
      let html = escapeHtml(value);
      html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>').replace(/^## (.+)$/gm, '<h3>$1</h3>').replace(/^# (.+)$/gm, '<h3>$1</h3>');
      html = html.replace(/\[([^\]]+)\]\((?:pages\/)?([a-z0-9]+(?:-[a-z0-9]+)*)\.md\)/g, '<a href="#page=$2">$1</a>');
      html = html.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>');
      return '<p>' + html + '</p>';
    };
    const state = (message) => `<div class="state">${escapeHtml(message)}</div>`;
    const renderList = (pages) => {
      list.innerHTML = pages.length ? pages.map(page => `<button class="page-card" data-slug="${escapeHtml(page.slug)}"><strong>${escapeHtml(page.title)}</strong><span>${escapeHtml(page.type)} · ${escapeHtml(page.status)}<br>${escapeHtml(page.summary)}</span></button>`).join('') : state('暂无已提交页面。');
      list.querySelectorAll('[data-slug]').forEach(button => button.addEventListener('click', () => openPage(button.dataset.slug)));
    };
    const openPage = async (slug) => {
      detail.innerHTML = state('正在读取…');
      try {
        const response = await fetch('/api/wiki/pages/' + encodeURIComponent(slug), {credentials:'same-origin'});
        if (response.status === 401) { location.href = '/auth/login'; return; }
        if (!response.ok) throw new Error('page');
        const result = await response.json();
        const page = result.page;
        detail.innerHTML = `<h2>${escapeHtml(page.title)}</h2><div class="meta"><span>类型：${escapeHtml(page.type)}</span><span>状态：${escapeHtml(page.status)}</span><span>更新：${escapeHtml(page.updated_at)}</span><span>版本：${escapeHtml(result.version)}</span></div><div class="body">${safeMarkdown(page.body)}</div><div class="sources">来源：${page.sources.map(escapeHtml).join('、')}</div>`;
        detail.querySelectorAll('a[href^="#page="]').forEach(link => link.addEventListener('click', (event) => { event.preventDefault(); openPage(link.getAttribute('href').slice(6)); }));
      } catch (error) { detail.innerHTML = state('页面读取失败，请稍后重试。'); }
    };
    const loadPages = async (url='/api/wiki/pages') => {
      list.innerHTML = state('正在读取…');
      try {
        const response = await fetch(url, {credentials:'same-origin'});
        if (response.status === 401) { location.href = '/auth/login'; return; }
        if (!response.ok) throw new Error('list');
        const result = await response.json(); renderList(result.pages);
        if (result.pages.length) openPage(result.pages[0].slug);
      } catch (error) { list.innerHTML = state('服务暂时不可用，请稍后重试。'); }
    };
    document.getElementById('search').addEventListener('submit', (event) => { event.preventDefault(); const query = new FormData(event.currentTarget).get('q'); loadPages(query ? '/api/wiki/search?q=' + encodeURIComponent(query) : '/api/wiki/pages'); });
    loadPages();
  </script>
</body>
</html>"""


class WikiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, app):
        self.app = app
        super().__init__(address, handler)


class WikiWebApp:
    def __init__(self, auth: AuthService, shared: SharedWikiService, local: RemoteWikiService | None = None):
        self.auth = auth
        self.shared = shared
        self.local = local or RemoteWikiService()
        self.max_body_bytes = int(os.environ.get("LLM_WIKI_MAX_REQUEST_BYTES", "524288"))

    def server(self, host: str = "127.0.0.1", port: int = 8000) -> WikiHTTPServer:
        return WikiHTTPServer((host, port), WikiRequestHandler, self)

    def _member(self, headers: dict[str, str]) -> dict[str, Any]:
        authorization = headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            return self.auth.authenticate_mcp_token(authorization[7:].strip())
        cookies = {}
        for item in headers.get("Cookie", "").split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookies[key] = value
        return self.auth.authenticate_session(cookies.get("lw_session", ""))

    @staticmethod
    def _json(body: bytes) -> Any:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError("Request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise StoreError("Request body must be a JSON object.")
        return value

    def get(self, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        parsed = urllib.parse.urlsplit(path)
        route = parsed.path
        if route in {"/healthz", "/api/health"}:
            result = {"status": "ok", "service": "llm-wiki", "checks": {"storage": "ok", "identity": "configured" if os.environ.get("MENTI_AUTH_CODE_URL") else "unconfigured", "model": "configured" if os.environ.get("LLM_WIKI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") else "unconfigured"}}
            return self.response(200, result)
        if route == "/auth/login":
            authorize = os.environ.get("MENTI_AUTHORIZE_URL", "").strip()
            if not authorize:
                raise StoreError("Menti login is not configured.")
            state = self.auth.store.issue_state()
            query = urllib.parse.urlencode({"response_type": "code", "client_id": os.environ.get("MENTI_CLIENT_ID", ""), "redirect_uri": os.environ.get("MENTI_REDIRECT_URI", ""), "state": state})
            return 302, {"Location": authorize + ("&" if "?" in authorize else "?") + query}, b""
        if route == "/auth/callback":
            query = urllib.parse.parse_qs(parsed.query)
            state = query.get("state", [""])[0]
            self.auth.store.consume_state(state)
            login = self.auth.login_with_code(query.get("code", [""])[0])
            return 302, {"Location": "/", "Set-Cookie": f"lw_session={login['session_token']}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={self.auth.session_ttl}"}, b""
        if route == "/":
            self._member(headers)
            return 200, {"Content-Type": "text/html; charset=utf-8"}, READER_HTML.encode("utf-8")
        member = self._member(headers)
        if route == "/api/wiki/status":
            return self.response(200, self.shared.status(member["subject"]))
        if route == "/api/wiki/pages":
            result = self.shared.store.list_pages()
            return self.response(200, result)
        if route == "/api/wiki/search":
            return self.response(200, self.shared.search(member["subject"], urllib.parse.parse_qs(parsed.query).get("q", [""])[0]))
        match = re.fullmatch(r"/api/wiki/pages/([a-z0-9]+(?:-[a-z0-9]+)*)", route)
        if match:
            result = self.shared.page(member["subject"], match.group(1))
            if result["page"] is None:
                raise NotFoundError("Wiki page does not exist.")
            return self.response(200, result)
        match = re.fullmatch(r"/api/wiki/pages/([a-z0-9]+(?:-[a-z0-9]+)*)/versions", route)
        if match:
            return self.response(200, self.shared.versions(member["subject"], match.group(1)))
        if route == "/api/me":
            return self.response(200, {"member": member})
        raise NotFoundError("Route does not exist.")

    def post(self, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        member = self._member(headers)
        payload = self._json(body)
        if path == "/api/mcp-token":
            session = headers.get("Cookie", "")
            token = next((value.split("=", 1)[1] for value in session.split(";") if value.strip().startswith("lw_session=")), "")
            return self.response(200, self.auth.issue_mcp_token(token))
        if path == "/api/local/organize":
            return self.response(200, self.local.organize_local(payload.get("materials", []), payload.get("existing_pages", []), payload.get("purpose", "")))
        if path == "/api/wiki/submit":
            return self.response(200, self.shared.submit(member["subject"], payload.get("base_version"), payload.get("idempotency_key", ""), payload.get("materials", []), payload.get("purpose", ""), update=payload.get("update")))
        match = re.fullmatch(r"/api/wiki/pages/([a-z0-9]+(?:-[a-z0-9]+)*)/restore", path)
        if match:
            return self.response(200, self.shared.restore(member["subject"], match.group(1), payload.get("version_id"), payload.get("base_version"), payload.get("idempotency_key", "")))
        if path == "/webhooks/menti/members":
            secret = os.environ.get("MENTI_WEBHOOK_SECRET", "")
            if not AuthService.verify_webhook(secret, body, headers.get("X-Menti-Signature", "")):
                raise AuthError("Webhook signature is invalid.")
            event = payload
            result = self.auth.apply_member_webhook(event.get("event_id", ""), event.get("member", event), int(event.get("sequence", 0)))
            return self.response(200, {"status": result})
        raise NotFoundError("Route does not exist.")

    @staticmethod
    def response(status: int, value: Any) -> tuple[int, dict[str, str], bytes]:
        return status, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(value, ensure_ascii=False).encode("utf-8")


class WikiRequestHandler(BaseHTTPRequestHandler):
    server: WikiHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _finish(self, result: tuple[int, dict[str, str], bytes]) -> None:
        status, headers, body = result
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if body:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, exc: Exception) -> tuple[int, dict[str, str], bytes]:
        if isinstance(exc, AuthError):
            return self.server.app.response(401, {"error": "unauthorized"})
        if isinstance(exc, NotFoundError):
            return self.server.app.response(404, {"error": "not_found"})
        if isinstance(exc, ConflictError):
            return self.server.app.response(409, {"error": "conflict", "current_version": exc.current_version})
        if isinstance(exc, ModelError):
            return self.server.app.response(502, {"error": "model_unavailable"})
        if isinstance(exc, (StoreError, ValueError)):
            return self.server.app.response(400, {"error": str(exc)})
        return self.server.app.response(500, {"error": "internal_error"})

    def do_GET(self) -> None:
        try:
            self._finish(self.server.app.get(self.path, {key: value for key, value in self.headers.items()}))
        except Exception as exc:
            self._finish(self._error(exc))

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > self.server.app.max_body_bytes:
                raise StoreError("Request body exceeds the configured limit.")
            body = self.rfile.read(length)
            self._finish(self.server.app.post(self.path.split("?", 1)[0], {key: value for key, value in self.headers.items()}, body))
        except Exception as exc:
            self._finish(self._error(exc))

