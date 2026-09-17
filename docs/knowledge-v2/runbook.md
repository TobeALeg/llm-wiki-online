# 切换与回退 runbook

状态：本次实施随代码交付，未在真实生产上执行过切换。

## 1. 切换前必须确认

| 检查项 | 命令 | 通过条件 |
|---|---|---|
| 全量测试 | `python -m unittest discover -s tests` | 无失败 |
| 用例映射 | `python -m unittest tests.test_acceptance_case_map` | 56/56 覆盖，无未知 ID |
| 分发副本一致 | `python scripts/sync_skill_distribution.py --check` | 无 stale 文件 |
| 评测门槛 | `python evals/knowledge_v2/run_eval.py --check-gold` | 打印缺口；有缺口则不得声称质量达标 |

## 2. 写入点

v2 的权威写入点只有一个：`ClaimStore.commit_changes`。它的输入是
`ValidatedKnowledgeChangeSet`，事务内写入 claim 与版本、origin、evidence 连接、支持需求、
关系、topic、review queue、审计、幂等结果、知识版本号与投影 dirty 标记。

v1 的 `SharedWikiStore.commit_update` 仍然存在，只在 v2 关闭时可达。开启 v2 后
`SharedWikiService.submit` 拒绝预先构造的 `pages[]`，`restore` 拒绝直接恢复页面。

开关：环境变量 `LLM_WIKI_KNOWLEDGE_V2`，取 `1`/`true`/`yes`/`on` 为开启。默认关闭。

## 3. 切换步骤

1. 停止写入口（MCP 与 Web 均可继续只读）。
2. 用 SQLite 备份 API 生成一致快照，**不是文件复制**：
   `python -c "import sys; sys.path.insert(0,'plugins/llm-wiki'); from llm_wiki_mcp.migrate_v2 import backup_database; print(backup_database(source='data/lw.sqlite3', destination='backups/lw-<date>.sqlite3'))"`
3. 记录快照的 sha256 与当时的知识版本号。
4. 在**副本**上跑迁移 dry run：`plan_migration(...)`，把 `gaps` 与计数与 v1 行数核对。
5. 在副本上跑真实迁移 `migrate(...)`，核对 `verify_restore(...)` 的 evidence 计数。
6. 副本验收通过后，对生产库执行同一迁移。
7. 设 `LLM_WIKI_KNOWLEDGE_V2=1` 并重启写入口。
8. 校验：`knowledge_status` 报告的知识版本与迁移报告一致，且若干固定问题能答对。

迁移可重入：中断后用同一 `checkpoint` 预算再次运行会从游标继续，不重复插入。

## 4. 回退

回退分两种，不能混为一谈。

### 4.1 切换后还没有 v2 新写入

恢复备份即可。`rollback_plan(...)` 的 `options` 会给出 `restore_backup`。

步骤：停写 → 停写后确认无 v2 新写入（`v2_writes_since_backup == 0`）→ 恢复快照 →
重启 v1 → 校验 v1 版本号与快照时一致。

### 4.2 切换后已有 v2 新写入

**旧数据库不包含这些新写入。** 恢复旧备份会丢掉它们，这不是无损回滚。

`rollback_plan(...)` 在这时会给出 `read_only_fallback` 与 `replay_from_log`，并列出
`new_write_ids`。可选做法：

- 只读回退：v1 继续服务，v2 库保留，v2 写入在这段时间不可用。新写入仍在 v2 库里，可查。
- 按日志重放：用 `new_write_ids` 与 `stage_artifacts` 里的阶段产物把 v2 期间的改动按材料
  重新摄取回 v1 之前的状态。这需要人工确认每一条，不是自动过程。

不得声称"翻一个开关就能无损回滚全部 v2 写入"。

## 5. 备份与恢复演练

`backup_database` 用 `sqlite3.Connection.backup` 生成一致快照，返回字节数与 sha256。
`restore_database` 恢复到目标路径，目标已存在时默认拒绝覆盖，需要 `overwrite=True`。

`verify_restore` 不只检查数据库能打开：它报告项目数、来源数、claim 数与当前知识版本，
并对每一条 evidence 调用 `ClaimStore.load_evidence`，返回成功与失败计数及失败清单。
只有 `readable is True` 且 `evidence_failed == 0` 才算恢复可用。

演练必须实际执行一次并留下记录；只读通过的备份不算演练过。

## 6. 故障时的行为

| 故障 | 行为 |
|---|---|
| 模型调用失败 | run 进入 `failed`，批次记为 unfinished，不读成 completed |
| 提交中途失败 | 整个事务回滚，无半提交，幂等键不消耗 |
| 提交成功后响应丢失 | 同键重试命中已提交结果；base_version 前进后仍按同意图命中 |
| 投影失败 | 知识已提交，状态为 `committed_projection_pending`，查询回退到当前 claim 的保守模板 |
| 来源撤回 | claim 保留，支持状态重算，证据恢复返回 `SOURCE_WITHDRAWN` |
| artifact 损坏 | 返回 `HASH_MISMATCH`，不生成替代原文 |
| 审核对象已变 | `StaleReviewError` 带当前版本，审核保持 open |
| 上游指令注入 | `screen_material` 标出并记录，不执行、不改变状态 |
