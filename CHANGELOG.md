# 变更记录

## Knowledge Architecture v2

### 新的知识层

Claim 成为权威状态，链路是 `SourceRevision → ParsedArtifact → EvidenceRef → Claim →
PageProjection`。Page 由已提交的 claim 版本按固定模板渲染，manifest 记录用了哪些版本。

对使用者的影响：Wiki 页面现在每条实质性内容都带状态标签（已采纳 / 提议，未采纳 / 假设 /
已替代 / 已撤回 / 系统推导 / 依据待复核 / 依据不足），可以追问某句话的原文出处而得到
冻结快照里的确切文字，也可以追问某个结论为什么成立而分别看到"当时记录的理由"和"系统推导
的解释"。页面不再是一段无法追溯来源的文字。

### 本地 `/lw` 新增命令

| 命令 | 作用 |
|---|---|
| `knowledge-init` | 把当前项目绑定到本地知识空间 |
| `knowledge-prepare` | 冻结材料并打印分块、证据 ID 与历史 Claim，供 agent 阅读 |
| `knowledge-ingest` | 摄取材料；`--candidates FILE` 用 agent 写好的候选，`--dry-run` 只报告不提交 |
| `knowledge-status` | 知识版本、各类型 claim 数、待审数量、数据库位置 |
| `knowledge-search` | 同时检索页面与 claim，按 claim 去重 |
| `knowledge-evidence` | 从一个引用恢复确切原文 |
| `knowledge-claim` | 一个 claim 的状态、来源归因、支持组、历史版本、关系 |
| `knowledge-explain` | 为什么持有这个结论 |
| `knowledge-reviews` / `knowledge-review` | 列出待审项 / 记录审核动作 |
| `knowledge-export` | 把渲染好的页面写成 `.llm-wiki/pages/*.md`；手改过的页面不覆盖，标记为 manual 并提示把新增内容作为材料重新摄取 |

已有的 `init`、`scan`、`ingest`、`update`、`status`、`lint`、`context` 行为不变。

### 本地存储位置

本地的权威知识库放在 `LLM_WIKI_HOME`（默认 `~/.llm-wiki`）下的 `knowledge.sqlite3`，
一台机器一个库，多个项目注册在同一个库里，这样跨项目的 Topic 身份才成立。各项目的
`.llm-wiki/binding.json` 保留项目绑定，`.llm-wiki/pages/` 保留 Markdown 投影。

对使用者的影响：删除一个 checkout 不会删除知识；`~/.llm-wiki` 现在需要跟其他重要数据
一起备份。

### 旧写入接口的变化

开启 `LLM_WIKI_KNOWLEDGE_V2=1` 后：

- 带 `pages[]` 的提交被明确拒绝。直接写入页面文字会让没有 claim 支撑的内容进入 Wiki。
  错误信息指向 `ingest`。
- 直接恢复历史页面被拒绝。恢复页面文字会让被替代的措辞回到页面上而替换它的 claim 不动。
  改用 `restore_as_manual_note`，把历史措辞作为材料重新摄取，并标注它是事后的追述。
- 旧的读接口继续工作，v1 数据保留。

开关默认关闭，因此升级本身不改变现有行为。

### 新增的审核动作

`retain`、`edit`、`reject`、`adopt_decision`、`confirm_supersession`、`confirm_identity`。
每个动作改变什么、不改变什么写在 `docs/knowledge-v2/contract.md` 第 9 节。

要点：`retain` 只记录保留意图，不会把假设变成已验证、不会把建议变成决定。

### 评测与验收

- `evals/knowledge_v2/acceptance_cases.json` 是 56 项用例契约；
  `case_map.json` 由测试生成，报告每个用例由哪些测试证明。
- `evals/knowledge_v2/metrics.py` 是第 8.2 节十二个指标的算法，阈值固定在代码里。
- `evals/knowledge_v2/gold.jsonl` 目前只含构造边界组，全部标注
  `label_status: constructed_unconfirmed`。真实材料组的 gold 仍需人工确认。

### 未完成的发布门槛

- 真实模型 holdout 三次运行未执行（缺少 API key），报告里标注 `not_executed`。
- 真实材料 gold 未人工确认，规模未达第 8.1 节最低要求，缺口由
  `run_eval.py --check-gold` 打印。

这两项属于 P7 的发布签收，不能由机器签收。
