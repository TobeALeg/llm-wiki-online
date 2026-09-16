# LLM Wiki Company MCP

LLM Wiki Company MCP 是公司共享 Wiki 的受保护远程 MCP 服务。所有启用的 mentti
成员读取和维护同一份已提交知识；本地文件不会被自动扫描或上传。

## 服务地址

```text
https://lw.app.mentti.work/mcp
```

传输协议为 MCP Streamable HTTP。推荐使用 OAuth 2.1；不支持 OAuth 的终端客户端可以
使用网页生成的个人 MCP Key。

## 推荐：OAuth 2.1

支持 MCP OAuth 的客户端只需添加服务地址，然后完成一次 mentti 浏览器授权。客户端保存
refresh token 后会静默续期，不需要每次使用都重新登录。

```bash
codex mcp add lw-company --url https://lw.app.mentti.work/mcp
codex mcp login lw-company
```

服务公开 OAuth Protected Resource Metadata、Authorization Server Metadata、动态客户端
注册、Authorization Code + PKCE 和轮换式 refresh token。

## 备选：个人 MCP Key

1. 登录 `https://lw.app.mentti.work/mcp/setup`。
2. 点击“生成 Key”，立即复制页面只显示一次的 `lw_pat_...`。
3. 在启动 Codex 的终端中配置：

```bash
read -s LW_MCP_TOKEN && export LW_MCP_TOKEN
codex mcp add lw-company \
  --url https://lw.app.mentti.work/mcp \
  --bearer-token-env-var LW_MCP_TOKEN
```

Key 与个人 mentti 身份绑定，可以在 `/mcp/setup` 撤销；成员被停用后已有 Key 也会失效。
不要把 Key 写入仓库、聊天记录或共享文档。

## 能力与数据边界

- `company_wiki_status`、`company_wiki_search`、`company_wiki_page`：读取公司 Wiki。
- `company_wiki_versions`、`company_wiki_restore`：查看和恢复版本。
- `company_wiki_submit`：明确提交选定材料到公司 Wiki。
- `local_wiki_organize`：整理调用方明确提供的材料但不在本服务持久化；材料仍会发送给
  配置的模型提供商。
- `company_wiki_revoke_credential`：撤销当前调用使用的凭证。

写入使用 `base_version` 防止静默覆盖，并使用 `idempotency_key` 保证安全重试。工具执行
失败会通过 MCP 的 `result.isError` 返回；身份失败使用 HTTP 401。

## 验证

```bash
curl -sS https://lw.app.mentti.work/.well-known/oauth-protected-resource/mcp
curl -sS https://lw.app.mentti.work/.well-known/oauth-authorization-server
```

健康检查位于 `https://lw.app.mentti.work/healthz`，只返回存储、身份和模型配置状态。
