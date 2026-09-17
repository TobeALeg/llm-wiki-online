# LLMWiki Knowledge Architecture v2 发布报告

日期：2026-09-17
分支：`knowledge-v2`
基线：`ba0d4bd8eee5692f6be0bc83c73b3b0babdc1d52`

本报告区分**已实测**与**未执行**。未执行的项目写成未执行，不写成通过。

## 1. 交付内容

| 模块 | 行数 | 作用 |
|---|---:|---|
| `knowledge_types.py` | 811 | Claim/Origin/关系/支持组/Scope 的数据契约与状态合法性 |
| `evidence.py` | 519 | 文本规范化、artifact 冻结、span 寻址与校验、原话恢复 |
| `chunking.py` | 701 | 结构分块；code point 坐标；evidence/context spans；render recipe |
| `claim_store.py` | 3156 | v2 SQL、版本、原子提交、支持组、关系、血缘、REVIEW、投影持久化 |
| `knowledge_service.py` | 743 | 冻结→提取→提交→投影→检索的统一用例 |
| `knowledge_pipeline.py` | 2284 | discovery/synthesis/grounding/value 编排，注入式模型角色 |
| `projection.py` | 694 | 确定性页面模板、manifest、dirty 判定、人工编辑检测 |
| `retrieval.py` | 1264 | CJK 分词、Page/Claim 双检索、去重、Why Chain、source fallback |
| `migrate_v2.py` | 1788 | v1→v2 迁移、legacy 映射、备份/恢复/回退计划 |
| `wiki_prompts.py` | 345 | 五个角色的契约、指令与版本号 |
| `model_roles.py` | 104 | 按角色解析模型；按供应商上报记录 usage，缺报记 unknown |

行数由 `wc -l` 实测。

接入：`shared_service.py` 增加 v2 读路径与 `ingest`；`remote_mcp.py` 增加 5 个 v2 工具；
`webapp.py` 增加 5 条 v2 只读路由；`skills/lw/scripts/wiki.py` 增加 9 个 `knowledge-*`
子命令。

分发：v2 核心模块只用标准库，副本由 `scripts/sync_skill_distribution.py` 生成，
`tests/test_skill_distribution_parity.py` 守住字节一致与依赖边界。

## 2. 工程不变量

全部 56 项用例已映射到真实测试并由测试自己生成报告。

| 组 | 用例 | 状态 |
|---|---|---|
| Evidence | E01–E08 | 通过 |
| Schema 与范围 | M01–M03 | 通过 |
| 提取质量 | K01–K11 | 通过（受控 fake 模型） |
| 身份、演化、来源失效 | R01–R08 | 通过 |
| REVIEW | V01–V03 | 通过 |
| Page、查询、Why | Q01–Q10 | 通过 |
| 事务与重试 | T01–T06 | 通过 |
| 入口与安全边界 | X01–X04 | 通过 |
| 迁移与恢复 | G01–G03 | 通过 |

复现：`python -m unittest tests.test_acceptance_case_map`，报告在
`evals/knowledge_v2/case_map.json`。

## 3. 本次修掉的真实缺陷

这些都是先由测试或独立复核发现、再在**根因处**修掉的，不是绕过去。

1. **id 宽度契约自相矛盾。** `evidence.make_evidence` 与 `freeze_artifact` 用完整 SHA-256
   摘要生成 64 位十六进制地址，而 `knowledge_types` 的 pattern 只收 32 位，于是自己铸造的
   地址无法被自己的校验引用。修法：pattern 同时接受两种宽度，并删掉流水线里为绕开它而做的
   截断与别名映射。
2. **幂等键在版本前进后把重试判成不同请求。** 进程在"提交成功"与"记录成功"之间崩溃后，
   带着刷新过的 `base_version` 重试会命中 `IdempotencyError`，且永远重试不成功。修法：
   `knowledge_submissions` 增加不含 `base_version` 的 `intent_hash`，同意图重试取回已提交
   结果；不同意图仍然拒绝。
3. **审核动作完全不可达。** `review_action` 查询 `review_decisions.request_hash`，而建表语句
   没有该列，任何审核动作都抛 `OperationalError`。修法：补列、写入该列，并为旧库加
   `_add_missing_columns`。
4. **未知顶层字段的 ChangeSet 被部分写入。** 一个夹带 `pages[]` 的 ChangeSet 会让旁边的
   claim 照常提交，v1 的写入形状因此可以从 v2 的入口溜进来。修法：顶层字段白名单，未知字段
   整体拒绝。
5. **未注册 run 时提交直接失败。** 带 `dropped` 候选的 ChangeSet 写进 `stage_artifacts`，
   而该表的 `run_id` 外键指向 `ingest_runs`，于是没有先 `create_run` 的调用整次提交被外键
   挡住。修法：DROP 是知识判断不是运行产物，改记进独立的 `dispositions` 表。
6. **审核动作写了一条多余的运行产物审计。** `review_decisions` 本身就是完整审计，而那条
   多余的 `stage_artifacts` 写入正是让未注册 run 的审核永远无法解决的原因。删掉冗余写入。
7. **生产 MCP 从没注册出 v2 工具。** `create_remote_mcp` 增加了 v2 参数，但
   `create_company_mcp` 没有传，线上五个 v2 工具全部不可达。修法：工厂构造 v2 store 并传
   全部回调，适配层测试断言这些工具存在。
8. **同名的两个 `EvidenceError`。** `knowledge_types` 与 `evidence` 各定义一个，于是 Web 层
   捕获的是另一个类，证据错误会漏成 500。修法：合成一个类，由 `knowledge_types` 定义、
   `evidence` 再导出。
9. **Web 的 v2 路由 pattern 只收 32 位十六进制**，而 evidence id 是 64 位，该路由对真实 id
   永远匹配不上，只对不存在的 id 匹配。修法：同时接受两种宽度。

另外修掉一个测试自身的资源泄漏（Windows 上删不掉被占用的临时数据库），它是基线里唯一的
未解释失败。

## 4. 已实测的关键行为

- 引用恢复：`[start, end)` 按 code point 恢复中文、emoji、组合字符与 CRLF 文本；按字节或
  UTF-16 计算的偏移会恢复出错误文本或抛 `INVALID_SPAN`。
- 重建格式与原文分离：拆分后的表格片段只引用数据行，表头与分隔行是 context；补造的代码围栏
  不在任何可引用范围里。
- 失败关闭：artifact 被篡改返回 `HASH_MISMATCH`，span 越界返回 `INVALID_SPAN`，来源撤回返回
  `SOURCE_WITHDRAWN`，都不返回替代文本。
- 零容忍：无记录就写 `adopted` 或 `verified` 被拒绝，且不静默降级。
- 支持组：撤回一个来源后，独立支持组仍让 claim 保持 grounded；全部撤回后为 unsupported，
  claim 不被删除也不被断言为假。
- 模式一致：同一材料、同一受控提取在 Local 与 Shared 得到相同语义 ChangeSet；本地材料不出现在
  Shared 数据库字节里。
- 原子提交：在写入点注入失败后，claim、版本、origin、关系、审核、版本号与幂等记录都不留痕。
- 迁移可重入：丢失迁移记账后重跑不产生重复；备份用 SQLite 备份 API，恢复后逐条校验 evidence
  仍能恢复原文。
- 干净安装：在一个 `llm_wiki_mcp` 不可导入的子进程里，`/lw` 的 v2 命令可用，且第二次运行能读到
  第一次存下的知识。

## 5. 未执行，因此不作为通过

| 签收项 | 状态 | 原因 |
|---|---|---|
| 三次真实模型 holdout 均达 blocking 门槛 | 未执行 | 无 API key |
| 所有零容忍错误在三次运行中为 0 | 未执行 | 同上 |
| v1/v2 同口径价值与覆盖对照 | 未实现 | 需要真实模型跑 v1 与 v2 |
| 真实材料 gold 达第 8.1 节最低规模并人工确认 | 未完成 | 机器不能给自己签收 |
| 用户或独立验收者签收质量标签与 REVIEW 负担 | 待签收 | 需要人 |

当前 gold 规模与门槛的差距由 `python evals/knowledge_v2/run_eval.py --check-gold` 实测打印，
缺口未在本文里复制成一张好看的表。

## 6. 已知局限

以下条目是实施中确认未做或与规格原设计不同的地方，逐条列出而不是留给读者去发现。

### 6.1 与规格原设计的偏离

| 规格位置 | 原设计 | 实际实现 | 理由 |
|---|---|---|---|
| §5.1 `core.py` | v2 校验 Claim、scope、来源、关系、ChangeSet | `core.py` 与基线逐字节相同，v2 校验在 `knowledge_types.py` | 规格同时禁止把 `run_routed` 改成表面返回 pages 内部写 Claim 的函数；v1 的页面链路保持原样，v2 契约放在新模块 |
| §5.1 `store.py` | 委托 v2 提交与读取 | `store.py` 与基线逐字节相同，委托由 `knowledge_service.py` 与 `shared_service.py` 承担 | 同上；两套写入语义共用一个 v1 模块会互相渗透 |
| §5.1 `wiki_pipeline.py` | 增加 v2 prepare/extract/validate | v2 编排在 `knowledge_pipeline.py` | `wiki_pipeline.py` 的 `run_routed` 是 v1 页面合并，规格明确要求不要改它 |
| §5.2 `schema_migrations` | 迁移版本、基线、校验和、恢复清单 | `claim_store.py` 写入 `schema_migrations` 版本 2 与 schema 校验和；`migrate_v2.py` 的进度记在 `migration_runs` | v2 建表是同一次 `initialize()`，没有编号迁移步骤；旧库补列走 `_add_missing_columns`，未编号 |
| §8.4 REVIEW 比例与成本 | 每材料组 token、重试与费用统计 | `metrics.review_burden` 提供比例、每材料组待审与合组卡片数；成本按阶段记录在 `_stage`，没有汇总报告 | 成本需要真实模型运行才有数，未执行前汇总只会是空表 |

### 6.2 未实现的规格条款

| 规格位置 | 要求 | 状态 |
|---|---|---|
| §4.5 | 自动修复上限每阶段一次；传输重试与语义修复分别计数 | 未实现。批次失败会记入 `run_items` 并把 run 停在非 completed，但没有重试计数，也没有每阶段修复上限 |
| §8.3 | v1/v2 同口径覆盖率与复用率提升 10 个百分点 | 未实现，需要真实模型跑 v1 与 v2 |
| §8.4 | 每保留 Claim 的 token、重试与费用 | 阶段级已记录（模型、prompt 版本、重试次数、上报 token 或 unknown），未按 Claim 汇总 |

### 6.3 设计与实现的局限

- 页面分组规则只有两条：显式 `page_slug` 与 topic 标签。没有路由到任何 topic 的 claim 落在
  `project-knowledge` 页，保证可读，但没有按主题聚类。已存在页面的 claim 归属从它的 manifest
  读回，所以重命名或重建不会把 claim 丢到别的页面。
- `retrieval.py` 的嵌入通道是可替换的可选件，未配置时 `degraded` 为 true 并给出原因。当前只有
  词面检索路径经过实测。
- 迁移只处理 v1 的 sources 与 pages；v1 的 audits 与 versions 存进 `legacy_generated_page`
  的 structure，不作为可引用证据。
- `evals/knowledge_v2/gold.jsonl` 现有 25 条构造边界组，全部标注
  `label_status: constructed_unconfirmed`；真实材料组尚未建立，缺口由 `--check-gold` 打印。
- K01–K11 与 X01 用受控 fake 与受控提取器验证语义，不衡量提取质量；只有未执行的 holdout 会。
- 浏览器写权限边界未扩大：审核动作经 MCP/CLI，Web 只读。
- 本地模式的投影导出是显式命令（`knowledge-export`），不在摄取后自动执行。手改过的页面会被
  标记为 manual 并在导出时跳过，`knowledge-status` 不区分 manual 与 stale。
- `.scratch/knowledge-v2/` 里的补丁脚本是本次实施的工作产物。整文件提交加脚本拼接，意味着单次
  改动不能逐行回溯。

## 7. 回退

见 `docs/knowledge-v2/runbook.md`。要点：切换前可恢复备份；v2 已有新写入后，恢复旧备份会
丢掉这些写入，只能只读回退或按日志重放。不提供无损回滚承诺。
