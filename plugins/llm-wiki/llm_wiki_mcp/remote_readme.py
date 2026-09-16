"""User-facing instructions for the protected company MCP endpoint.

The text is kept as a module constant rather than a sibling `.md` file so that it
is guaranteed to ship inside the wheel; the deployed image only copies
`plugins/llm-wiki`, and a missing package-data entry would turn the route into a
500 on the production host.
"""

REMOTE_MCP_README = r"""# LLM Wiki 远程 MCP 接入说明

本页说明如何在 Codex、Claude Code 等 Agent 客户端里注册并调用公司共享 Wiki 的远程
MCP 服务。内容对应服务端实际实现；如与客户端界面不一致，以本文的请求示例为准。

## 服务入口

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| 远程 MCP | `https://lw.app.mentti.work/mcp` | 供 Agent 调用，需要 Bearer 凭证 |
| 浏览器阅读 | `https://lw.app.mentti.work/` | Menti 登录后只读浏览，不提供写入 |
| 健康检查 | `https://lw.app.mentti.work/healthz` | 无需登录，只返回 storage/identity/model 状态 |

传输方式为 MCP Streamable HTTP，无状态、返回 JSON（非 SSE）。凭证使用 Bearer，不是
OAuth 授权码流程 —— 见下文「已知限制」。

## 前提

- 拥有**已启用**的 Menti 成员身份。被停用的成员即使持有未过期凭证也会被立即拒绝。
- 浏览器能完成一次 Menti 登录，用于取得凭证。
- 只能访问公司共享 Wiki 这一份存储；接口不接受调用方指定的服务器路径，也不提供
  个人知识空间。

## 第一步：取得 MCP 凭证

目前没有在网页上提供一键复制按钮，需要手动取一次 `lw_session` 会话 Cookie，再用它
换取长期凭证。两个步骤都只在本机执行，不要把结果写进仓库或共享文档。

1. 用浏览器打开 `https://lw.app.mentti.work/` 完成 Menti 登录。
2. 打开开发者工具 → Application/存储 → Cookies → `https://lw.app.mentti.work`，
   复制 `lw_session` 的值。该 Cookie 是 HttpOnly，只能用这种方式读取。
3. 用会话 Cookie 换取 MCP 凭证：

```bash
export LW_SESSION='粘贴 lw_session 的值'
curl -sS -X POST https://lw.app.mentti.work/api/mcp-token \
  -H "Cookie: lw_session=$LW_SESSION" | tee /tmp/lw-token.json
```

返回：

```json
{
  "access_token": "……",
  "token_type": "Bearer",
  "expires_at": 1792131358,
  "subject": "mentti-user-4"
}
```

把 `access_token` 存到本地环境变量，例如追加到 `~/.zshrc`（不要提交到仓库）：

```bash
export LW_MCP_TOKEN='粘贴 access_token 的值'
```

注意事项：

- **有效期 30 天**。过期后重复上面步骤重新签发。
- 凭证与 Menti 成员身份绑定，接口没有任何 `subject`/`email`/`name` 参数可以覆盖身份。
- 泄露或怀疑泄露时，调用 `company_wiki_revoke_credential` 立即吊销；停用成员后服务端
  也会直接拒绝其旧凭证。
- 会话 Cookie 本身 8 小时过期，只在换取凭证时用到。

## 第二步：在 Agent 客户端注册

### Codex CLI

```bash
codex mcp add lw-company \
  --url https://lw.app.mentti.work/mcp \
  --bearer-token-env-var LW_MCP_TOKEN
codex mcp list
```

`--bearer-token-env-var` 让 Codex 从环境变量读取凭证，因此启动 Codex 的 shell 里必须
先有 `LW_MCP_TOKEN`。写进 `~/.codex/config.toml` 的等价配置是：

```toml
[mcp_servers.lw-company]
url = "https://lw.app.mentti.work/mcp"
bearer_token_env_var = "LW_MCP_TOKEN"
```

### Claude Code

```bash
claude mcp add --transport http lw-company https://lw.app.mentti.work/mcp \
  --header "Authorization: Bearer $LW_MCP_TOKEN"
claude mcp list
```

`--header` 会把当时的字面值写入配置，所以凭证轮换后需要重新执行一次。

### 其他支持 Streamable HTTP 的客户端

在客户端的 MCP 配置里加一个类型为 `http`（部分客户端写 `streamable-http`）的服务，
URL 为 `https://lw.app.mentti.work/mcp`，请求头固定带 `Authorization: Bearer <token>`：

```json
{
  "mcpServers": {
    "lw-company": {
      "type": "http",
      "url": "https://lw.app.mentti.work/mcp",
      "headers": { "Authorization": "Bearer ${LW_MCP_TOKEN}" }
    }
  }
}
```

## 第三步：验证连接

先确认凭证可用（`initialize` 握手），再列出工具。这两步都不读业务数据：

```bash
curl -sS -X POST https://lw.app.mentti.work/mcp \
  -H "Authorization: Bearer $LW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18","capabilities":{},
        "clientInfo":{"name":"lw-check","version":"0"}}}'
```

正常返回 `serverInfo.name` 为 `llm-wiki-remote`。然后列出工具：

```bash
curl -sS -X POST https://lw.app.mentti.work/mcp \
  -H "Authorization: Bearer $LW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

调用一个工具（读取共享 Wiki 状态）：

```bash
curl -sS -X POST https://lw.app.mentti.work/mcp \
  -H "Authorization: Bearer $LW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
        "name":"company_wiki_status","arguments":{}}}'
```

返回的 `structuredContent` 中 `version` 是当前共享 Wiki 版本号，写入时要用到。
如果这里返回 401，说明凭证无效、已过期、已吊销，或成员已被停用。

## 工具清单

所有读取工具都作用于公司共享 Wiki 已提交的版本，与浏览器看到的内容一致。

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `local_wiki_organize` | `materials`, `existing_pages`, `purpose` | 整理选定的本地材料，只返回校验过的更新包，**不在服务器持久保存** |
| `company_wiki_status` | 无 | 当前版本号与页面数量 |
| `company_wiki_search` | `query`, `limit`（默认 20） | 按关键词检索页面，不区分大小写 |
| `company_wiki_page` | `slug` | 读取单个页面正文、来源与状态 |
| `company_wiki_versions` | `slug` | 列出该页面的历史版本 |
| `company_wiki_submit` | `base_version`, `idempotency_key`, `materials`, `purpose` | 整理材料并原子写入共享 Wiki |
| `company_wiki_restore` | `slug`, `version_id`, `base_version`, `idempotency_key` | 把历史版本恢复为新的已提交版本 |
| `company_wiki_revoke_credential` | 无 | 立即吊销本次调用所用的凭证 |

写入是全员可用的：任何已启用成员都能提交和恢复，不需要管理员审批。

## 参数契约

### materials（选定材料）

至少一条，`source_id` 不能重复，全部 `content` 合计不超过 18 万字符。只接受内容与来源
元数据，**不接受** `path`、`file_path`、`root`、`command`、`args` 这类字段。

```json
[
  {
    "source_id": "conversation:sqlite-choice",
    "kind": "conversation",
    "label": "关于存储选型的结论",
    "content": "选择 SQLite，因为部署只有单机、需要事务和在线备份……"
  }
]
```

### existing_pages（本地整理时提供现有上下文）

只用于 `local_wiki_organize`，最多 500 页、合计 24 万字符：

```json
[
  { "slug": "storage-choice", "title": "存储选型", "body": "当前使用 SQLite……" }
]
```

### base_version 与冲突

`company_wiki_submit` 和 `company_wiki_restore` 都必须传当前版本号。版本号不等于服务端
当前值时，工具返回 `isError: true`，文案里带上服务端当前版本（例如
`Wiki changed since base_version 0; retry from version 3.`）。正确做法是重新读取状态、
基于新版本重新整理后提交，不要盲目重试同一个 `base_version`。

### idempotency_key

网络中断后安全重试的关键。格式为 `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`，同一次提交要
用同一个值。同一个 key 配不同的请求内容会报错；相同的 key 与内容则直接返回首次结果，
不会重复生成记录。

### purpose

本次整理目的，最长 8000 字符。会作为来源信息进入共享 Wiki。

## 调用示例

### 提交一条共享知识

```bash
curl -sS -X POST https://lw.app.mentti.work/mcp \
  -H "Authorization: Bearer $LW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
        "name":"company_wiki_submit",
        "arguments":{
          "base_version":0,
          "idempotency_key":"submit-2026-09-16-storage",
          "purpose":"记录存储选型的结论和理由",
          "materials":[{
            "source_id":"conversation:sqlite-choice",
            "kind":"conversation",
            "label":"存储选型结论",
            "content":"选择 SQLite：单机部署、需要事务和在线一致性备份。"
          }]
        }}}'
```

### 只整理、不写入

把 `company_wiki_submit` 换成 `local_wiki_organize`，参数去掉 `base_version` 和
`idempotency_key`，`materials` 后面再加 `existing_pages`。返回值是更新包，由调用方
Agent 自己写回本地；服务端**不保存请求正文与结果**。

注意「不落盘」只指不在服务器持久化，材料仍会经服务转发给配置的模型提供商
（当前为 `deepseek-flash`）。

### 恢复历史版本

先 `company_wiki_versions` 找到 `version_id`，再：

```bash
-d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{
      "name":"company_wiki_restore",
      "arguments":{"slug":"storage-choice","version_id":3,
                   "base_version":7,"idempotency_key":"restore-2026-09-16"}}}'
```

恢复不会删除历史，而是把旧内容作为**新的**已提交版本写回。

## 错误与排查

这里有一个**容易误判的地方**：MCP 协议下，工具执行失败仍然是 HTTP 200，错误写在
返回体的 `result.isError` 里，错误信息在 `result.content[0].text`。只有身份校验失败才是
真正的 HTTP 401。浏览器 API 的 409/502 状态码**不出现在** `/mcp` 上。

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| HTTP 401 `invalid_token` | 凭证无效、过期、已吊销，或成员已停用 | 重新签发凭证；若刚被停用需联系管理员 |
| HTTP 421 | 客户端用了错误 Host | 必须访问 `lw.app.mentti.work`，不要直连容器端口 |
| 200 且 `isError:true`，文案含「Wiki changed since base_version」 | `base_version` 已过期（冲突） | 用文案里的 `retry from version N` 重新整理提交 |
| 200 且 `isError:true`，文案含「Idempotency key was already used」 | 同一个 key 配了不同内容 | 换新的 `idempotency_key`，或改用原内容重放 |
| 200 且 `isError:true`，文案含「must contain content」「not paths or commands」「At least one selected material」 | 参数不符合契约 | 按文案修正 `materials` |
| 200 且 `isError:true`，文案含「No model provider key」「model」相关 | 模型提供商失败 | 稍后重试；与身份问题无关 |
| 200 且 `isError:true`，文案含「member is disabled」 | 成员已被停用 | 联系管理员，重新签发无效 |

判断调用是否成功，请检查 `result.isError`，不要只看 HTTP 状态码。冲突文案里会给出服务端
当前版本号，直接用它作为新的 `base_version` 即可，不要盲目重试原值。

`company_wiki_page` 查不到页面时**不是错误**：它返回成功，但 `page` 为 `null`，请据此判断
页面是否存在。`company_wiki_search` 只按标题、摘要、正文和标签做不区分大小写的关键词
匹配，查询词最长 200 字符，`limit` 会被限制在 1–50。

服务端日志与 `/healthz` 只记录状态，不记录正文、来源内容和凭证。

## 安全须知

- 凭证等价于你的身份，不要粘贴到聊天、issue、仓库或共享文档；一人一份，不要共用。
- 不要把 `lw_session` 或 `access_token` 写进脚本后再提交。
- 只有明确要共享的内容才调用 `company_wiki_submit`；本地材料不会自动进入公司 Wiki。
- 只读场景优先只用读取工具，避免误写。

## 已知限制

- **不支持 MCP OAuth 自动发现。** 服务端签发的是预置 Bearer 凭证，没有实现
  `/.well-known/oauth-protected-resource` 等授权服务器元数据。会自动走 OAuth 的客户端
  （例如 ChatGPT 网页版自定义连接器）无法直接连接本服务，请使用支持自定义请求头的
  Agent 客户端。
- 网页端只有只读浏览，没有凭证生成按钮（见第一步的手动流程）。
- 浏览器阅读与 MCP 读取同一份已提交版本，不存在两个入口展示不一致的情况。
- 停用成员的通知存在传播延迟，服务端默认每 15 分钟兜底对账一次。
"""


def readme_bytes() -> bytes:
    """Return the Markdown document as UTF-8 bytes for the HTTP response."""

    return REMOTE_MCP_README.encode("utf-8")
