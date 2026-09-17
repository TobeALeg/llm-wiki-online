# ADR 0003：v2 核心只用标准库，分发副本由脚本生成

状态：已采纳

## 背景

`/lw` 在开发机上通过安装好的 `llm_wiki_mcp` 包工作，但在别人的机器上只有技能包。技能包
里没有任何 pip 依赖，所以一个 import 了服务器包的 `/lw` 会在最需要它的地方失败，也就是
在没有开发环境的干净安装上。

技能目录已经有两份镜像（`skills/lw/` 与 `plugins/llm-wiki/skills/lw/`），而且 `chunking.py`
出现在第三处（`llm_wiki_mcp/chunking.py`）。手工同步三份代码必然会漂移。

## 决策

v2 核心模块只用标准库：

`knowledge_types.py`、`evidence.py`、`chunking.py`、`claim_store.py`、
`knowledge_service.py`、`knowledge_pipeline.py`、`projection.py`、`retrieval.py`、
`migrate_v2.py`、`wiki_prompts.py`。

需要兄弟模块时用同一段 shim，让三份副本字节相同：

```python
try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .knowledge_types import Claim
except ImportError:
    from knowledge_types import Claim
```

分发副本由 `scripts/sync_skill_distribution.py` 生成。`skills/lw/` 是唯一的技能树来源，
`plugins/llm-wiki/skills/lw/` 由它镜像；v2 模块从 `plugins/llm-wiki/llm_wiki_mcp/` 拷进两棵
技能树的 `scripts/`。

`tests/test_skill_distribution_parity.py` 读同一个生成器的模块列表，断言每一份副本与
canonical 字节相同，并断言每个 vendored 模块只 import 标准库或同为 vendored 的兄弟。

## 后果

干净安装不需要 `mcp` 包，`/lw` 的本地知识路径可用。测试
`tests/test_modes_and_adapters.py::test_the_vendored_core_loads_without_the_server_package`
在一个 `llm_wiki_mcp` 不可导入的子进程里验证这一点。

新增 v2 模块时必须同时把它加入 `VENDORED_MODULES` 并运行同步脚本，否则 parity 测试失败。

本地权威 Store 放在 `LLM_WIKI_HOME`（默认 `~/.llm-wiki`）下的一个 `knowledge.sqlite3`，
各项目的 `.llm-wiki/binding.json` 保留项目绑定，`.llm-wiki/pages/` 保留 Markdown 投影。多个
项目注册在同一个本地 Store 里，跨项目 Topic identity 才成立；每个项目孤立建库会得到
每项目独立的 Topic，不是共享身份。
