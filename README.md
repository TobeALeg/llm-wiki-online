# LLM Wiki

LLM Wiki 将项目文件和经过选择的 Agent 对话整理成带来源记录的项目知识库，并逐步扩展为支持远程 MCP、公司共享 Wiki 和在线浏览的服务。

## 当前内容

- `skills/lw/`：可显式调用的 `LLM Wiki` Agent Skill，命令名为 `/lw`。
- `plugins/llm-wiki/`：本地 MCP 服务、Agent Plugin manifest 和 bundled skill。
- `tests/test_llm_wiki.py`：本地 Wiki CLI 的行为测试。
- `tests/test_llm_wiki_mcp.py`：本地 MCP 服务的行为测试。
- `.scratch/lw-remote-shared-wiki/spec.md`：远程 MCP、公司共享 Wiki 和在线浏览规格。

远程服务、Menti 身份接入、公司 Wiki 持久化和在线前端尚未实施；spec 是实施依据，不代表已经部署。

当前已包含本地 MCP 服务，可通过 stdio 连接 Codex，也可以绑定到本机 loopback HTTP 端口供本地 tunnel 使用。它不是远程公司 Wiki 服务，也不应直接暴露到公网。安装、项目白名单和 MCP 工具说明见 [`plugins/llm-wiki/README.md`](plugins/llm-wiki/README.md)。

## 本地 CLI

安装 skill 后，在目标项目目录中使用：

```bash
export DEEPSEEK_API_KEY="..."
python ~/.agents/skills/lw/scripts/wiki.py init
python ~/.agents/skills/lw/scripts/wiki.py update --episode "本轮对话里需要长期保留的结论"
python ~/.agents/skills/lw/scripts/wiki.py context "要查询的项目知识"
```

模型默认为 `deepseek-flash`，API 地址默认为 `https://api.deepseek.com`。项目只需按需编辑 `.llm-wiki/purpose.md`；目录、页面索引和来源记录由 skill 自动管理。

可用命令包括：

```text
/lw              # 初始化（如需要）并更新 Wiki
/lw init         # 只初始化
/lw status       # 查看待处理内容
/lw ask 为什么选择 SQLite
/lw scan
/lw lint
```

界面中显示为 `LLM Wiki`。技能注册名 `lw` 关闭了自动触发，只有显式输入 `/lw` 或 `$lw` 时才会运行。

## 安装 Agent Skill

Codex 和其他 Agent Skills-compatible 客户端可以从本仓库安装：

```bash
mkdir -p ~/.agents/skills
cp -R skills/lw ~/.agents/skills/
```

Claude Code 的安装位置是 `~/.claude/skills/lw/`。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

## 目录结构

```text
skills/lw/
├── SKILL.md
├── agents/openai.yaml
├── references/architecture.md
└── scripts/wiki.py

plugins/llm-wiki/
├── plugin.json
├── mcp.json
├── skills/lw/
├── llm_wiki_mcp/
└── README.md

tests/test_llm_wiki.py
tests/test_llm_wiki_mcp.py
.scratch/lw-remote-shared-wiki/spec.md
```
