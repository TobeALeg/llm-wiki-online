# LLM Wiki Company MCP

LLM Wiki Company MCP 是 mentti 公司成员共同维护的远程知识库。服务通过 MCP
Streamable HTTP 提供读取、检索、整理、提交、版本和恢复工具；网页用于阅读 Wiki、
生成个人 MCP Key 和管理凭证。

## 安装

MCP 地址：

```text
https://lw.app.mentti.work/mcp
```

### OAuth 2.1（推荐）

```bash
codex mcp add lw-company --url https://lw.app.mentti.work/mcp
codex mcp login lw-company
```

首次连接会打开 mentti 登录与授权页。客户端保存 refresh token 后会静默续期，不会在
每次使用时重复打开浏览器。

### 个人 MCP Key

不支持 OAuth 的客户端使用静态 Bearer Key：

1. 登录 `https://lw.app.mentti.work/connect`。
2. 生成并立即复制只显示一次的 `lw_pat_...` Key。
3. 在启动 Codex 的终端中执行：

```bash
read -s LW_MCP_TOKEN && export LW_MCP_TOKEN
codex mcp add lw-company \
  --url https://lw.app.mentti.work/mcp \
  --bearer-token-env-var LW_MCP_TOKEN
```

Key 与个人 mentti 身份绑定，可在生成页面撤销；成员停用后已有 Key 和 OAuth 凭证都不能
继续访问。不要把凭证写入仓库、聊天记录或共享文档。

## 工具

- `company_wiki_status`、`company_wiki_search`、`company_wiki_page`
- `company_wiki_versions`、`company_wiki_restore`
- `company_wiki_submit`
- `local_wiki_organize`
- `company_wiki_revoke_credential`

公司写入必须显式调用 `company_wiki_submit`。`local_wiki_organize` 不在本服务持久化材料，
但材料仍会发送给配置的模型提供商。详细参数和错误语义见登录后的
`https://lw.app.mentti.work/readme.md`。

## OAuth 发现

```text
https://lw.app.mentti.work/.well-known/oauth-protected-resource/mcp
https://lw.app.mentti.work/.well-known/oauth-authorization-server
```

服务实现 Authorization Code + PKCE、动态客户端注册、短期 access token、轮换式
refresh token 和 token revocation。mentti 仍是身份来源，lw 作为 MCP 的 OAuth 2.1
授权服务器适配层签发仅面向 `/mcp` 的凭证。

## 数据与运维

- 公司 Wiki 使用固定共享范围，不接受客户端指定服务器路径。
- 页面、来源、版本、审计和幂等记录在同一个 SQLite 事务中提交。
- 生产配置与验收见 [`docs/operations.md`](docs/operations.md)。
- 模块与数据流见 [`docs/architecture.md`](docs/architecture.md)。

开发验证：

```bash
python3 -m unittest discover -s tests -v
```
