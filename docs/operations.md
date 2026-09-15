# 生产运行与验收

## 配置边界

在服务器的部署目录创建 `.env.production`，参照仓库中的
`.env.production.example` 填入 Menti 动态应用和模型提供商配置。client secret、
Webhook secret 和模型 key 只放在该文件或部署平台 secret store，不放进代码、镜像、
GitHub issue 或提交记录。

生产数据只位于 Docker volume `lw-data` 中的
`/var/lib/llm-wiki/lw.sqlite3`。web 和 mcp 进程共享这一个数据库；更新镜像和重启
不会初始化或清空它。

## 部署顺序

1. 在 Menti 动态应用登记回调地址
   `https://lw.app.mentti.work/auth/callback` 和成员 Webhook 地址。
2. 配置 `lw.app.mentti.work` DNS，再分别验证服务器 80/443、TLS 证书和 Nginx
   `/mcp` 反向代理。Menti 登记、DNS、TLS 和代理不是同一个验收项。
3. 在服务器部署 Nginx 配置和 Compose 服务，执行 `docker compose up -d --build --wait`，再确认 `/healthz` 返回 `status=ok`。
4. 检查 `/healthz`、登录回调、`/api/mcp-token` 和远程 `/mcp` 的真实调用。

Compose 将 web 和 mcp 仅绑定到宿主机回环地址 8000/4310，宿主机 Nginx 使用这两个
地址转发；它们不会直接暴露到公网。若使用 GitHub Actions 部署，还需配置
`LW_DEPLOY_HOST`、`LW_DEPLOY_USER`、`LW_DEPLOY_PATH`、`LW_DEPLOY_SSH_KEY` 和
由可信运维来源提供的完整 `LW_DEPLOY_KNOWN_HOSTS` SSH 主机指纹。

## 备份和恢复

使用 operator CLI 对 SQLite 做在线一致性备份：

```text
llm-wiki-ops backup /var/lib/llm-wiki/lw.sqlite3 /var/backups/llm-wiki/wiki.sqlite3
llm-wiki-ops integrity /var/backups/llm-wiki/wiki.sqlite3
llm-wiki-ops restore /var/backups/llm-wiki/wiki.sqlite3 /var/lib/llm-wiki/restore-test.sqlite3
```

恢复演练应先写入隔离路径，检查页面、来源、版本和审计数量，再安排短暂停机
替换生产文件。恢复操作不会删除备份源；替换生产文件是运维人员的明确操作。

## 观测

`/healthz` 只返回 storage、identity、model 的状态，不返回正文、来源内容、token
或 provider response。HTTP 401 表示身份/凭证问题，502 表示模型 provider 问题，
409 表示版本冲突，4xx 表示输入或来源契约问题。

成员 Webhook 处理后立即拒绝停用成员的旧 session 和 MCP token。正常通知延迟取决
于 Menti Webhook 传递；默认每 15 分钟通过 `MENTI_MEMBERS_URL` 做一次兜底对账。

## 真实上线验收

模拟模型测试只证明协议、事务和安全边界，不证明真实整理质量。可用上线前必须
使用授权的代表性材料完成：Menti 登录、真实 Codex/WorkBuddy MCP 只读调用、本地
不落盘、两名成员共享读写、冲突、幂等、历史恢复、停用通知、服务重启保留数据、
备份隔离恢复，以及 HTTPS/匿名拒绝检查。

本地代码和模拟测试通过不等于已完成线上部署；只有上述外部身份、服务器和真实
材料验收都通过后，才可声明 `lw.app.mentti.work` 可用上线。
