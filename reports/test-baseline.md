# 测试基线与本次实测结果

## 变更前基线（commit `ba0d4bd`）

命令：`python -m unittest discover -s tests`

| 项 | 值 |
|---|---|
| 用例数 | 175 |
| 通过 | 173 |
| 失败 | 2 |
| 环境 | Python 3.13.0, Windows 10.0.26200 |

两个失败项及其原因，实测得到，不是引用提交说明：

1. `test_remote_mcp` 导入失败：`ModuleNotFoundError: No module named 'mcp'`。这是环境缺口，
   CI 里由 `pip install -e ./plugins/llm-wiki` 提供该依赖。本地装上 `mcp` 后消失。
2. `test_auth.AuthTests.test_existing_auth_database_is_migrated_without_losing_tokens` 在
   `tearDown` 抛 `PermissionError: [WinError 32]`，删不掉被占用的 `legacy.sqlite3`。根因是
   测试自己用 `with sqlite3.connect(...)` 建库与查询，该上下文管理器提交但不关闭连接，
   Windows 上文件保持锁定。测试缺陷，不是被测代码缺陷。本次一并修掉（改用
   `contextlib.closing`）。

## 本次实测结果

命令：`python -m unittest discover -s tests`

| 项 | 值 |
|---|---|
| 用例数 | 298 |
| 通过 | 298 |
| 失败 | 0 |
| 跳过 | 0 |

`python -m unittest tests.test_acceptance_case_map`：56 项用例契约全部被真实测试声明，
无未知 ID。报告由测试自己写成 `evals/knowledge_v2/case_map.json`。

`python scripts/sync_skill_distribution.py --check`：全部生成副本与 canonical 字节一致。

## 未执行的部分

以下两项需要真实模型或人工判断，本次环境不具备条件，**不作为已通过**：

| 项 | 原因 | 复现命令 |
|---|---|---|
| 真实模型 holdout 全链路三次运行 | 无 API key | `python evals/knowledge_v2/run_eval.py --split holdout --runs 3 --materials <dir>` |
| 真实材料 gold 人工确认 | 机器不能给自己签收 | `python evals/knowledge_v2/run_eval.py --check-gold` |

当前 gold 规模与第 8.1 节最低要求的差距（`--check-gold` 实测输出）：

| 最低要求 | 现有 | 需要 |
|---|---:|---:|
| 材料组 | 12 | 30 |
| must_keep | 13 | 60 |
| decision / constraint | 0 | 20 |
| 负例 | 12 | 20 |
| 合法综合目标 | 2 | 10 |
| 未来问题 | 19 | 30 |
| holdout must_keep | 5 | 20 |
| holdout decision / constraint | 0 | 10 |
| holdout 负例 | 5 | 10 |
| holdout 综合目标 | 0 | 5 |
| holdout 未来问题 | 8 | 10 |

现有 25 条全部标注 `label_status: constructed_unconfirmed`，是构造边界组，不冒充真实用户
数据，也不冒充人工确认。
