"""User-facing onboarding for LLM Wiki Company MCP."""

REMOTE_MCP_README = r"""# LLM Wiki Company MCP

公司共享 Wiki 的远程 MCP 地址：

```text
https://lw.app.mentti.work/mcp
```

## 推荐安装：OAuth 2.1

支持 MCP OAuth 的客户端只需添加服务地址并完成一次 mentti 浏览器授权。客户端保存并
轮换 refresh token，后续不会重复唤起登录。

```bash
codex mcp add lw-company --url https://lw.app.mentti.work/mcp
codex mcp login lw-company
```

## 终端安装：个人 MCP Key

不支持 OAuth 的客户端可使用静态 Bearer Key：

1. 登录 <https://lw.app.mentti.work/connect>。
2. 生成并立即复制页面只显示一次的 `lw_pat_...` Key。
3. 在启动客户端的终端中配置：

```bash
read -s LW_MCP_TOKEN && export LW_MCP_TOKEN
codex mcp add lw-company \
  --url https://lw.app.mentti.work/mcp \
  --bearer-token-env-var LW_MCP_TOKEN
```

Key 与个人 mentti 身份绑定，可以在生成页面撤销；成员被停用后已有 Key 也会失效。
不要把 Key 写入仓库、聊天记录或共享文档。

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `company_wiki_projects` | 查看可用 Project |
| `company_wiki_create_project` | 创建 Project |
| `company_wiki_status` | 按 `project_id` 读取版本与页面数量 |
| `company_wiki_search` | 按 `project_id` 搜索已提交页面 |
| `company_wiki_page` | 按 `project_id` 读取页面正文、来源与状态 |
| `company_wiki_versions` | 按 `project_id` 查看页面历史 |
| `company_wiki_submit` | 按 `project_id` 提交选定材料；需要 `base_version` 和 `idempotency_key` |
| `company_wiki_restore` | 按 `project_id` 将历史版本恢复为新的已提交版本 |
| `local_wiki_organize` | 整理选定材料但不在本服务持久化 |
| `company_wiki_revoke_credential` | 撤销当前调用使用的凭证 |

`local_wiki_organize` 的“不持久化”不等于“不出网”：材料仍会发送给配置的模型提供商。
公司写入必须显式调用 `company_wiki_submit`，本地材料不会自动进入共享 Wiki。
默认 Project ID 是 `company`；建议先调用 `company_wiki_projects`，再将返回的 Project ID
传给其他公司 Wiki 工具。不同 Project 的页面、来源、版本和审计记录彼此隔离。

工具执行失败通过 MCP `result.isError` 返回；身份失败使用 HTTP 401。写入冲突时先重新读取
`company_wiki_status`，基于新版本整理后再提交，不要盲目重放旧 `base_version`。

## 发现与健康检查

- Protected Resource Metadata：`/.well-known/oauth-protected-resource/mcp`
- Authorization Server Metadata：`/.well-known/oauth-authorization-server`
- 健康检查：`/healthz`
"""


def readme_bytes() -> bytes:
    return REMOTE_MCP_README.encode("utf-8")
