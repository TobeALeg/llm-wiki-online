# LLM Wiki plugin, shared service, and local MCP server

This plugin exposes project-scoped local Wikis to Codex over stdio and a protected
company Wiki over Streamable HTTP MCP. Project files remain local. The local
allowlist is used by local tools; the company service accepts selected content and
never a caller-provided server path. Its single bundled skill keeps the explicit
`/lw` name.

## Install

Python 3.10 or newer is required.

### Windows PowerShell

```powershell
git clone https://github.com/TobeALeg/llm-wiki-online.git
cd llm-wiki-online
py -m venv .venv
.\.venv\Scripts\python -m pip install -e .\plugins\llm-wiki
$env:DEEPSEEK_API_KEY = "your-key"
```

### macOS or Linux

```bash
git clone https://github.com/TobeALeg/llm-wiki-online.git
cd llm-wiki-online
python3 -m venv .venv
.venv/bin/python -m pip install -e ./plugins/llm-wiki
export DEEPSEEK_API_KEY="your-key"
```

## Allow a project

Choose a short project ID. The MCP tools receive this ID, never an arbitrary local path.

```powershell
.\.venv\Scripts\llm-wiki-projects.exe add my-project "D:\work\my-project" --init
.\.venv\Scripts\llm-wiki-projects.exe list
```

On macOS or Linux, use `.venv/bin/llm-wiki-projects` instead. Registry data is stored in `~/.llm-wiki/projects.json`. Set `LLM_WIKI_REGISTRY` to override that location.

## Run locally

For a direct local Codex connection, use stdio:

```bash
codex mcp add llm-wiki -- /absolute/path/to/llm-wiki-online/.venv/bin/llm-wiki-mcp
codex mcp list
```

On Windows, pass the absolute path to `.venv\Scripts\llm-wiki-mcp.exe`. Start Codex from a shell where `DEEPSEEK_API_KEY` is set.

For an HTTP endpoint reachable by a local tunnel client:

```bash
llm-wiki-mcp --transport streamable-http --host 127.0.0.1 --port 4310
```

The endpoint is `http://127.0.0.1:4310/mcp`. It intentionally binds to loopback and has no application-level authentication; do not bind it to a public interface.

## Run the protected company service

Set `LLM_WIKI_REMOTE=true`, `LLM_WIKI_DATABASE` and the Menti/model variables from
`.env.production.example`, then start the two entry points:

```bash
llm-wiki-web --host 127.0.0.1 --port 8000
llm-wiki-mcp --transport streamable-http --host 127.0.0.1 --port 4310
```

The browser API and `/mcp` share one SQLite database. The first login uses the
Menti authorization-code callback; an authenticated member then obtains a short-
lived, revocable MCP Bearer credential from `/api/mcp-token`. No subject or email
argument can override its identity.

## Connect ChatGPT web privately

1. Create a tunnel in OpenAI Platform tunnel settings and obtain its `tunnel_id` and runtime API key.
2. Install `tunnel-client` from the download link in those settings.
3. Point a tunnel profile at `http://127.0.0.1:4310/mcp`, run `doctor`, then keep the profile running.
4. In ChatGPT, enable Developer mode under **Settings → Security and login**. Create a plugin connection, choose **Tunnel**, and select the tunnel.

```bash
export CONTROL_PLANE_API_KEY="sk-..."
tunnel-client init --sample sample_mcp_stdio_local --profile llm-wiki --tunnel-id tunnel_... --mcp-server-url http://127.0.0.1:4310/mcp
tunnel-client doctor --profile llm-wiki --explain
tunnel-client run --profile llm-wiki
```

The local MCP server and `tunnel-client` must both remain running while ChatGPT uses the Wiki.

## MCP tools

- `list_wiki_projects`
- `initialize_wiki`
- `save_episode`
- `update_wiki`
- `query_wiki`
- `wiki_status`
- `scan_wiki`
- `get_wiki_page`
- `lint_wiki`

`save_episode` records selected conversation knowledge without paying for a model call. `update_wiki` consolidates pending project files and episodes with DeepSeek. The default model is `deepseek-flash`; override it with `LLM_WIKI_MODEL` or the API base with `LLM_WIKI_BASE_URL`.

The protected company MCP additionally exposes `local_wiki_organize` for a
selected local material set (it returns a validated package without writing to
the company database), plus `company_wiki_status`, `company_wiki_search`,
`company_wiki_page`, `company_wiki_versions`, `company_wiki_submit`, and
`company_wiki_restore`. Browser access is read-only; company writes go through
the authenticated MCP tools.
