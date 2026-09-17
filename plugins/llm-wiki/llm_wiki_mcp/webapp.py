"""Authenticated browser API and minimal static reader for the shared Wiki."""

from __future__ import annotations

import json
import hmac
import html
import os
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .auth import AuthError, AuthService
from .model import ModelError
from .oauth import OAuthError, OAuthService
from .remote_readme import readme_bytes
from .knowledge_types import EvidenceError
from .remote_service import RemoteWikiService
from .shared_service import SharedWikiService
from .store import DEFAULT_PROJECT_ID, ConflictError, PageNotFoundError, StoreError


class NotFoundError(RuntimeError):
    pass


# mentti's app directory generates its callback and webhook addresses from the app
# URL using a fixed convention and does not allow custom paths, so both the
# conventional and the original paths are served.
CALLBACK_PATHS = {"/auth/callback", "/api/auth/sso/callback"}
WEBHOOK_PATHS = {"/webhooks/menti/members", "/api/internal/menti/events"}

# The MCP onboarding document is browsable next to the reader, so it follows the
# reader's browser flow instead of the JSON 401 used by the fetch-based API.
README_PATHS = {"/readme.md", "/readme"}
PROTECTED_RESOURCE_PATHS = {
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
}


def event_ordering(sequence: Any, occurred_at: Any) -> int:
    """Order member events, falling back to mentti's `occurred_at` millisecond clock.

    mentti sends no numeric sequence, so a shared default would mark every event
    after the first as stale and freeze member state.
    """

    if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
        return sequence
    if isinstance(occurred_at, str) and occurred_at:
        try:
            parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
    return int(time.time() * 1000)


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
    header { display: flex; gap: 14px; align-items: center; padding: 22px max(24px, calc((100vw - 1180px) / 2)); background: #1f332b; color: #f9f7ef; }
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
    button, input, summary { font: inherit; }
    button { min-height: 40px; padding: 8px 14px; border-radius: 8px; white-space: nowrap; flex-shrink: 0; font-size: 14px; font-weight: 600; }
    button:focus-visible, summary:focus-visible, a:focus-visible { outline: 2px solid #a87925; outline-offset: 3px; }
    button:disabled { opacity: .5; cursor: wait; }
    header { justify-content: space-between; }
    header h1 { flex-shrink: 0; }
    #search { margin: 0; min-width: 0; }
    #search input { min-width: 0; border-radius: 8px; height: 40px; }
    main { grid-template-columns: 220px 1fr; align-items: start; }
    aside { display: flex; flex-direction: column; gap: 24px; }
    .section-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 12px; }
    .section-heading h2 { margin: 0; }
    .quiet { background: transparent; color: #62675f; }
    .quiet:hover { background: #efeee7; color: #20221f; }
    .project-list { display: grid; gap: 4px; }
    .project-button { width: 100%; text-align: left; white-space: normal; overflow-wrap: anywhere; background: transparent; color: #62675f; }
    .project-button[aria-current="true"] { background: #e5ece3; color: #244b38; }
    .page-card { white-space: normal; overflow-wrap: anywhere; }
    .page-card span { font-weight: 400; line-height: 1.5; }
    .settings { border-top: 1px solid #deddd3; padding-top: 16px; font-size: 14px; color: #62675f; }
    .settings summary { cursor: pointer; padding: 8px 0; }
    .settings a { display: block; padding: 8px 0; color: #62675f; text-decoration: none; }
    .settings a:hover { color: #275d48; text-decoration: underline; }
    dialog { width: min(420px, calc(100% - 32px)); border: 1px solid #deddd3; border-radius: 16px; padding: 28px; background: #fffef9; color: #20221f; }
    dialog::backdrop { background: #14251b66; }
    dialog h2 { margin: 0 0 24px; font-size: 22px; }
    #create-project { display: grid; gap: 12px; }
    #create-project input { border-radius: 8px; }
    .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
    #create-error { color: #a23529; margin: 0; font-size: 14px; }
    @media (max-width: 760px) { header { flex-wrap: wrap; } form { order: 2; flex-basis: 100%; } main { grid-template-columns: 1fr; margin-top: 18px; } article { min-height: 420px; } }
  </style>
</head>
<body>
  <header><h1>LLM Wiki</h1><form id="search" role="search"><input name="q" aria-label="搜索当前项目" placeholder="搜索当前项目" autocomplete="off"><button type="submit">搜索</button></form></header>
  <main><aside aria-label="Wiki 导航">
    <section><div class="section-heading"><h2>项目</h2><button id="new-project" class="quiet" type="button">＋ 新建</button></div><nav id="projects" class="project-list" aria-label="项目"></nav></section>
    <section><h2>页面目录</h2><div id="list" class="page-list"><div class="state">正在读取…</div></div></section>
    <details class="settings"><summary>设置</summary><a href="/connect">MCP Key 管理</a><a href="/readme.md">MCP 接入说明</a></details>
  </aside><article id="detail" aria-live="polite"><div class="state">请选择一个页面。</div></article></main>
  <dialog id="project-dialog" aria-labelledby="project-dialog-title"><h2 id="project-dialog-title">新建项目</h2><form id="create-project"><label for="project-name">项目名称</label><input id="project-name" name="name" required maxlength="120" autocomplete="off" autofocus><p id="create-error" role="alert" hidden></p><div class="dialog-actions"><button type="button" id="cancel-project" class="quiet">取消</button><button type="submit">创建项目</button></div></form></dialog>
  <script>
    const list = document.getElementById('list');
    const detail = document.getElementById('detail');
    const projectList = document.getElementById('projects');
    let listRequest = 0;
    let pageRequest = 0;
    let currentProject = 'company';
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
    const projectQuery = () => '?project_id=' + encodeURIComponent(currentProject);
    const openPage = async (slug) => {
      const request = ++pageRequest;
      detail.innerHTML = state('正在读取…');
      try {
        const response = await fetch('/api/wiki/pages/' + encodeURIComponent(slug) + projectQuery(), {credentials:'same-origin'});
        if (response.status === 401) { location.href = '/auth/login'; return; }
        if (!response.ok) throw new Error('page');
        const result = await response.json();
        if (request !== pageRequest) return;
        list.querySelectorAll('[data-slug]').forEach(button => button.classList.toggle('active', button.dataset.slug === slug));
        const page = result.page;
        detail.innerHTML = `<h2>${escapeHtml(page.title)}</h2><div class="meta"><span>类型：${escapeHtml(page.type)}</span><span>状态：${escapeHtml(page.status)}</span><span>更新：${escapeHtml(page.updated_at)}</span><span>版本：${escapeHtml(result.version)}</span></div><div class="body">${safeMarkdown(page.body)}</div><div class="sources">来源：${page.sources.map(escapeHtml).join('、')}</div>`;
        detail.querySelectorAll('a[href^="#page="]').forEach(link => link.addEventListener('click', (event) => { event.preventDefault(); openPage(link.getAttribute('href').slice(6)); }));
      } catch (error) { if (request !== pageRequest) return; detail.innerHTML = state('页面读取失败，请稍后重试。'); }
    };
    const loadPages = async (url='/api/wiki/pages' + projectQuery()) => {
      const request = ++listRequest;
      ++pageRequest;
      detail.innerHTML = state('正在读取…');
      list.innerHTML = state('正在读取…');
      try {
        const response = await fetch(url, {credentials:'same-origin'});
        if (response.status === 401) { location.href = '/auth/login'; return; }
        if (!response.ok) throw new Error('list');
        const result = await response.json();
        if (request !== listRequest) return;
        renderList(result.pages);
        if (!result.pages.length) detail.innerHTML = state(url.startsWith('/api/wiki/search') ? '没有找到匹配页面。' : '暂无已提交页面。');
        if (result.pages.length) openPage(result.pages[0].slug);
      } catch (error) { if (request !== listRequest) return; list.innerHTML = state('服务暂时不可用，请稍后重试。'); detail.innerHTML = state('无法读取页面。'); }
    };
    const loadProjects = async () => {
      const response = await fetch('/api/wiki/projects', {credentials:'same-origin'});
      if (!response.ok) throw new Error('projects');
      const result = await response.json();
      currentProject = result.projects.some(project => project.id === currentProject) ? currentProject : result.projects[0].id;
      projectList.innerHTML = result.projects.map(project => `<button type="button" class="project-button" data-project="${escapeHtml(project.id)}" aria-current="${project.id === currentProject}">${escapeHtml(project.name)}</button>`).join('');
      projectList.querySelectorAll('[data-project]').forEach(button => button.addEventListener('click', () => {
        currentProject = button.dataset.project;
        projectList.querySelectorAll('[data-project]').forEach(item => item.setAttribute('aria-current', String(item === button)));
        document.getElementById('search').reset();
        loadPages();
      }));
    };
    const projectDialog = document.getElementById('project-dialog');
    const createForm = document.getElementById('create-project');
    const createError = document.getElementById('create-error');
    document.getElementById('new-project').addEventListener('click', () => { createForm.reset(); createError.hidden = true; projectDialog.showModal(); });
    document.getElementById('cancel-project').addEventListener('click', () => projectDialog.close());
    createForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = new FormData(createForm).get('name').trim();
      if (!name) { createError.textContent = '请输入项目名称。'; createError.hidden = false; return; }
      const submit = createForm.querySelector('[type="submit"]');
      submit.disabled = true;
      createError.hidden = true;
      try {
        const response = await fetch('/api/wiki/projects', {method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin', body:JSON.stringify({name})});
        if (response.status === 401) { location.href = '/auth/login'; return; }
        if (!response.ok) throw new Error('create');
        const project = await response.json();
        currentProject = project.id;
        projectDialog.close();
        document.getElementById('search').reset();
        try { await loadProjects(); await loadPages(); }
        catch (error) { list.innerHTML = state('项目已创建，目录读取失败，请刷新页面。'); detail.innerHTML = ''; }
      } catch (error) { createError.textContent = '创建失败，请稍后重试。'; createError.hidden = false; }
      finally { submit.disabled = false; }
    });
    document.getElementById('search').addEventListener('submit', (event) => { event.preventDefault(); const query = new FormData(event.currentTarget).get('q'); loadPages(query ? '/api/wiki/search?q=' + encodeURIComponent(query) + '&project_id=' + encodeURIComponent(currentProject) : '/api/wiki/pages?project_id=' + encodeURIComponent(currentProject)); });
    loadProjects().then(loadPages).catch(() => { list.innerHTML = state('项目读取失败，请稍后重试。'); });
  </script>
</body>
</html>"""


MCP_SETUP_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>LLM Wiki Company MCP</title><style>
body{max-width:760px;margin:48px auto;padding:0 24px;font:16px/1.65 system-ui;background:#f6f5f1;color:#20221f}
main{background:#fffef9;border:1px solid #deddd3;border-radius:16px;padding:32px}button{padding:10px 16px;border:0;border-radius:999px;background:#275d48;color:white;font-weight:650;cursor:pointer}
input,pre{width:100%;box-sizing:border-box;padding:12px;border:1px solid #c9c9bd;border-radius:8px;background:white}pre{white-space:pre-wrap;word-break:break-all}.warning{color:#8a4b18}a{color:#275d48}
</style></head><body><main><p><a href="/">← 返回 Wiki</a></p><h1>Company MCP Key</h1>
<p>为不支持 OAuth 的终端客户端生成一个个人访问令牌。Key 只显示一次，并继承你的 mentti 成员身份。</p>
<label>名称 <input id="label" maxlength="120" value="My terminal"></label><p><button id="create">生成 Key</button></p>
<section id="result" hidden><p class="warning">现在复制；关闭页面后无法再次查看。</p><pre id="key"></pre><pre id="command"></pre></section>
<h2>已有 Key</h2><div id="credentials">正在读取…</div>
<script>
const createButton=document.getElementById('create'),labelInput=document.getElementById('label'),resultSection=document.getElementById('result'),keyOutput=document.getElementById('key'),commandOutput=document.getElementById('command'),credentials=document.getElementById('credentials');
const escapeHtml=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const r=await fetch('/api/mcp-credentials');if(r.status===401){location.href='/auth/login?return_to=%2Fconnect';return}const data=await r.json();credentials.innerHTML=data.credentials.length?data.credentials.map(c=>`<p><strong>${escapeHtml(c.label||c.token_kind)}</strong> · ${c.revoked_at?'已撤销':`有效至 ${new Date(c.expires_at*1000).toLocaleString()} <button data-id="${escapeHtml(c.credential_id)}">撤销</button>`}</p>`).join(''):'暂无 Key。';credentials.querySelectorAll('[data-id]').forEach(b=>b.onclick=async()=>{await fetch('/api/mcp-credentials/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({credential_id:b.dataset.id})});load()})}
createButton.onclick=async()=>{const r=await fetch('/api/mcp-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:labelInput.value})});const data=await r.json();if(!r.ok){alert('生成失败');return}keyOutput.textContent=data.access_token;commandOutput.textContent=`read -s LW_MCP_TOKEN && export LW_MCP_TOKEN\ncodex mcp add lw-company --url https://lw.app.mentti.work/mcp --bearer-token-env-var LW_MCP_TOKEN`;resultSection.hidden=false;load()};load();
</script></main></body></html>"""


def oauth_consent_html(request: dict[str, str]) -> bytes:
    client = html.escape(request["client_name"])
    scope = html.escape(request["scope"])
    pending = html.escape(request["pending_id"], quote=True)
    return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>授权 LLM Wiki MCP</title></head><body style=\"max-width:640px;margin:64px auto;padding:0 24px;font:16px/1.6 system-ui\"><h1>授权 Company MCP</h1><p><strong>{client}</strong> 请求访问公司 Wiki。</p><p>权限：<code>{scope}</code></p><form method=\"post\" action=\"/oauth/authorize\"><input type=\"hidden\" name=\"pending_id\" value=\"{pending}\"><button name=\"decision\" value=\"approve\">允许</button> <button name=\"decision\" value=\"deny\">拒绝</button></form></body></html>""".encode()


class WikiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, app):
        self.app = app
        super().__init__(address, handler)


PROTOCOL_VERSION = "wiki+knowledge/2"

_ID_DIGEST = r"[0-9a-f]{32}(?:[0-9a-f]{32})?"
"""32 or 64 hex characters. A store-assigned id is a uuid4 hex, a content-derived
address such as an evidence id is a full digest, and both are permanent."""


def knowledge_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Tag a v2 payload without reshaping it.

    A browser view that dropped an error code or a status axis would show a
    citation that looks missing rather than one that failed a hash check, so the
    store's answer is passed through and only labelled.
    """

    return {**payload, "protocol_version": PROTOCOL_VERSION}


class WikiWebApp:
    def __init__(self, auth: AuthService, shared: SharedWikiService, local: RemoteWikiService | None = None, oauth: OAuthService | None = None):
        self.auth = auth
        self.shared = shared
        self.local = local or RemoteWikiService()
        self.oauth = oauth or OAuthService(auth, issuer=os.environ.get("LLM_WIKI_PUBLIC_URL", "https://lw.app.mentti.work"))
        self.max_body_bytes = int(os.environ.get("LLM_WIKI_MAX_REQUEST_BYTES", "524288"))

    def server(self, host: str = "127.0.0.1", port: int = 8000) -> WikiHTTPServer:
        return WikiHTTPServer((host, port), WikiRequestHandler, self)

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str:
        wanted = name.lower()
        return next((value for key, value in headers.items() if key.lower() == wanted), "")

    def _member(self, headers: dict[str, str]) -> dict[str, Any]:
        authorization = self._header(headers, "Authorization")
        if authorization.lower().startswith("bearer "):
            return self.auth.authenticate_mcp_token(authorization[7:].strip())
        cookies = {}
        for item in self._header(headers, "Cookie").split(";"):
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
        project_id = urllib.parse.parse_qs(parsed.query).get("project_id", [DEFAULT_PROJECT_ID])[0]
        if route in PROTECTED_RESOURCE_PATHS:
            return self.response(200, self.oauth.protected_resource_metadata())
        if route == "/.well-known/oauth-authorization-server":
            return self.response(200, self.oauth.authorization_server_metadata())
        if route in {"/healthz", "/api/health"}:
            try:
                self.shared.store.current_version()
                storage = "ok"
            except Exception:
                storage = "error"
            identity = "configured" if all(os.environ.get(name) for name in ("MENTI_AUTHORIZE_URL", "MENTI_AUTH_CODE_URL", "MENTI_CLIENT_ID", "MENTI_CLIENT_SECRET")) else "unconfigured"
            model = "configured" if os.environ.get("LLM_WIKI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") else "unconfigured"
            checks = {"storage": storage, "identity": identity, "model": model}
            result = {"status": "ok" if all(value == "configured" or value == "ok" for value in checks.values()) else "degraded", "service": "llm-wiki", "checks": checks}
            return self.response(200, result)
        if route == "/auth/login":
            authorize = os.environ.get("MENTI_AUTHORIZE_URL", "").strip()
            if not authorize:
                raise StoreError("mentti login is not configured.")
            return_to = urllib.parse.parse_qs(parsed.query).get("return_to", ["/"])[0]
            if (
                not return_to.startswith("/") or return_to.startswith("//")
                or len(return_to) > 4096 or any(ord(char) < 32 for char in return_to)
            ):
                return_to = "/"
            state = self.auth.store.issue_state(return_to=return_to)
            query = urllib.parse.urlencode({"response_type": "code", "client_id": os.environ.get("MENTI_CLIENT_ID", ""), "redirect_uri": os.environ.get("MENTI_REDIRECT_URI", ""), "state": state})
            # Path=/ because mentti's registered callback lives under /api/auth/...,
            # so a narrower path would not be sent back on the callback request.
            return 302, {"Location": authorize + ("&" if "?" in authorize else "?") + query, "Set-Cookie": f"lw_oauth_state={state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=300"}, b""
        if route in CALLBACK_PATHS:
            query = urllib.parse.parse_qs(parsed.query)
            state = query.get("state", [""])[0]
            cookie_state = next((value.split("=", 1)[1] for value in self._header(headers, "Cookie").split(";") if value.strip().startswith("lw_oauth_state=")), "")
            if not cookie_state or not hmac.compare_digest(cookie_state, state):
                raise AuthError("OAuth state is invalid.")
            return_to = self.auth.store.consume_state(state)
            login = self.auth.login_with_code(query.get("code", [""])[0])
            return 302, {"Location": return_to, "Set-Cookie": f"lw_session={login['session_token']}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={self.auth.session_ttl}"}, b""
        if route == "/oauth/authorize":
            try:
                member = self._member(headers)
            except AuthError:
                target = route + ("?" + parsed.query if parsed.query else "")
                return 302, {"Location": "/auth/login?" + urllib.parse.urlencode({"return_to": target})}, b""
            params = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            request = self.oauth.begin_authorization(params, member["subject"])
            return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, oauth_consent_html(request)
        if route == "/" or route in README_PATHS or route in {"/connect", "/mcp/setup"}:
            # A browser lands here straight from the mentti app directory, so an
            # anonymous visitor must be sent into the login flow. The JSON 401
            # below is only correct for the fetch-based API routes.
            try:
                self._member(headers)
            except AuthError:
                return 302, {"Location": "/auth/login"}, b""
            if route in README_PATHS:
                return 200, {"Content-Type": "text/markdown; charset=utf-8"}, readme_bytes()
            if route in {"/connect", "/mcp/setup"}:
                return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, MCP_SETUP_HTML.encode("utf-8")
            return 200, {"Content-Type": "text/html; charset=utf-8"}, READER_HTML.encode("utf-8")
        member = self._member(headers)
        if route == "/api/wiki/projects":
            return self.response(200, self.shared.projects(member["subject"]))
        if route == "/api/wiki/status":
            return self.response(200, self.shared.status(member["subject"], project_id))
        if route == "/api/wiki/pages":
            result = self.shared.store.list_pages(project_id)
            return self.response(200, result)
        if route == "/api/wiki/search":
            return self.response(200, self.shared.search(member["subject"], urllib.parse.parse_qs(parsed.query).get("q", [""])[0], project_id=project_id))
        match = re.fullmatch(r"/api/wiki/pages/([a-z0-9]+(?:-[a-z0-9]+)*)", route)
        if match:
            result = self.shared.page(member["subject"], match.group(1), project_id)
            if result["page"] is None:
                raise NotFoundError("Wiki page does not exist.")
            return self.response(200, result)
        match = re.fullmatch(r"/api/wiki/pages/([a-z0-9]+(?:-[a-z0-9]+)*)/versions", route)
        if match:
            try:
                return self.response(200, self.shared.versions(member["subject"], match.group(1), project_id))
            except PageNotFoundError as exc:
                raise NotFoundError(str(exc)) from exc
        if route == "/api/knowledge/status":
            return self.response(200, knowledge_result(self.shared.status(member["subject"], project_id)))
        if route == "/api/knowledge/search":
            return self.response(
                200,
                knowledge_result(
                    self.shared.search(
                        member["subject"],
                        urllib.parse.parse_qs(parsed.query).get("q", [""])[0],
                        project_id=project_id,
                    )
                ),
            )
        if route == "/api/knowledge/reviews":
            return self.response(200, knowledge_result(self.shared.reviews(member["subject"], project_id)))
        match = re.fullmatch(r"/api/knowledge/claims/(clm_" + _ID_DIGEST + r")", route)
        if match:
            version = urllib.parse.parse_qs(parsed.query).get("version", [""])[0]
            return self.response(
                200,
                knowledge_result(
                    self.shared.claim(
                        member["subject"],
                        match.group(1),
                        version=int(version) if version.isdigit() else None,
                        project_id=project_id,
                    )
                ),
            )
        match = re.fullmatch(r"/api/knowledge/evidence/(evd_" + _ID_DIGEST + r")", route)
        if match:
            try:
                return self.response(200, knowledge_result(self.shared.evidence(member["subject"], match.group(1), project_id)))
            except EvidenceError as exc:
                # A citation that cannot be recovered carries its code and no text.
                return self.response(409, {"error": exc.code, "reason": str(exc), "protocol_version": PROTOCOL_VERSION})
        match = re.fullmatch(r"/api/knowledge/why/(clm_" + _ID_DIGEST + r")", route)
        if match:
            depth = urllib.parse.parse_qs(parsed.query).get("depth", ["3"])[0]
            return self.response(
                200,
                knowledge_result(
                    self.shared.explain(
                        member["subject"],
                        match.group(1),
                        max_depth=int(depth) if depth.isdigit() else 3,
                        project_id=project_id,
                    )
                ),
            )
        if route == "/api/me":
            return self.response(200, {"member": member})
        if route == "/api/mcp-credentials":
            session = self._session_token(headers)
            return self.response(200, {"credentials": self.auth.list_credentials(session)})
        raise NotFoundError("Route does not exist.")

    def _session_token(self, headers: dict[str, str]) -> str:
        return next(
            (value.split("=", 1)[1] for value in self._header(headers, "Cookie").split(";") if value.strip().startswith("lw_session=")),
            "",
        )

    @staticmethod
    def _form(body: bytes) -> dict[str, str]:
        try:
            return {key: values[0] for key, values in urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
        except UnicodeDecodeError as exc:
            raise OAuthError("invalid_request", "Form body must be UTF-8.") from exc

    def post(self, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        if path == "/oauth/register":
            return self.response(201, self.oauth.register_client(self._json(body)))
        if path == "/oauth/token":
            return self.response(200, self.oauth.exchange_token(self._form(body)), extra_headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        if path == "/oauth/revoke":
            self.oauth.revoke_token(self._form(body))
            return 200, {"Cache-Control": "no-store"}, b""
        if path in WEBHOOK_PATHS:
            secret = os.environ.get("MENTI_WEBHOOK_SECRET", "")
            if not AuthService.verify_webhook(
                secret,
                body,
                self._header(headers, "X-Menti-Signature"),
                self._header(headers, "X-Menti-Timestamp"),
            ):
                raise AuthError("Webhook signature is invalid.")
            event = self._json(body)
            event_id = str(event.get("event_id") or self._header(headers, "X-Menti-Event-Id"))
            member = event.get("member", event)
            result = self.auth.apply_member_webhook(
                event_id, member, event_ordering(event.get("sequence"), event.get("occurred_at"))
            )
            return self.response(200, {"status": result})
        member = self._member(headers)
        if path == "/oauth/authorize":
            form = self._form(body)
            location = self.oauth.finish_authorization(
                form.get("pending_id", ""), member["subject"], form.get("decision") == "approve"
            )
            return 302, {"Location": location, "Cache-Control": "no-store"}, b""
        payload = self._json(body)
        if path == "/api/wiki/projects":
            return self.response(201, self.shared.create_project(member["subject"], str(payload["id"]) if "id" in payload else "project-" + uuid.uuid4().hex, str(payload.get("name", ""))))
        if path == "/api/mcp-token":
            return self.response(200, self.auth.issue_mcp_token(self._session_token(headers), str(payload.get("label", "Terminal"))))
        if path == "/api/mcp-credentials/revoke":
            revoked = self.auth.revoke_credential(self._session_token(headers), str(payload.get("credential_id", "")))
            return self.response(200, {"revoked": revoked})
        if path == "/api/local/organize":
            return self.response(200, self.local.organize_local(payload.get("materials", []), payload.get("existing_pages", []), payload.get("purpose", "")))
        raise NotFoundError("Route does not exist.")

    @staticmethod
    def response(status: int, value: Any, *, extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(extra_headers or {})
        return status, headers, json.dumps(value, ensure_ascii=False).encode("utf-8")


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
        if isinstance(exc, OAuthError):
            return self.server.app.response(exc.status, {"error": exc.error, "error_description": exc.description}, extra_headers={"Cache-Control": "no-store"})
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
            if length < 0 or length > self.server.app.max_body_bytes:
                raise StoreError("Request body exceeds the configured limit.")
            body = self.rfile.read(length)
            self._finish(self.server.app.post(self.path.split("?", 1)[0], {key: value for key, value in self.headers.items()}, body))
        except Exception as exc:
            self._finish(self._error(exc))


def main(argv: list[str] | None = None) -> None:
    import argparse
    from .auth import AuthStore, MentiIdentityProvider
    from .reconcile import MemberReconciler
    from .server import database_path
    from .shared_service import SharedWikiService
    from .store import SharedWikiStore

    parser = argparse.ArgumentParser(description="Run the authenticated LLM Wiki browser API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args(argv)
    database = database_path()
    provider = MentiIdentityProvider()
    auth = AuthService(AuthStore(database), provider)
    shared = SharedWikiService(SharedWikiStore(database))
    reconciler = None
    if os.environ.get("MENTI_MEMBERS_URL", "").strip():
        reconciler = MemberReconciler(auth, provider.list_members, interval_seconds=int(os.environ.get("LLM_WIKI_RECONCILE_INTERVAL_SECONDS", "900")))
        reconciler.start()
    server = WikiWebApp(auth, shared).server(args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if reconciler:
            reconciler.stop()
