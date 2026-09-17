# LLMWiki Knowledge Architecture v2 实施计划

状态：实施中

规格来源（本文的实现契约，优先级高于本计划的任何摘要）：

- `C:\Users\admin\Downloads\LLMWiki-v2-Implementation-Plan-and-Acceptance.md`
- `C:\Users\admin\Downloads\LLMWiki-v2-Acceptance-Cases.json`（56 项用例）

## 完成定义

可证伪的判据，全部跑在真实产物上：

1. `python -m unittest discover -s tests` 全绿，且不含未解释的失败。
2. 56 个用例 ID 每一个都映射到至少一个真实测试函数，且该测试通过。映射由
   `tests/test_acceptance_case_map.py` 守住；用例 ID 未映射、或映射到的测试不存在时失败。
3. `plugins/llm-wiki/llm_wiki_mcp/` 下的 v2 核心模块与 `skills/lw/scripts/` 下的分发副本
   字节一致（沿用既有 parity 测试的模式）。
4. 证据可恢复率、原话准确率的固定测试集 100% 通过；零容忍错误项在测试中有明确断言。
5. `evals/knowledge_v2/` 有 manifest、gold、指标计算脚本，能对同一批材料算出第 8.2 节的
   全部指标并输出整数分子/分母。

第 8 节里需要真实模型三次运行的门槛（P7）不在判据 1–5 内。它需要 API key，属于发布签收
项，报告里如实标注 `not_executed` 而不是写成通过。

## 本次不做

- 真实模型 holdout 三次运行（缺 API key，报告标注未执行）。
- 图可视化、Neo4j、分布式队列、学习排序。

## 数据形状（先定形状，再写逻辑）

链路 `SourceRevision → ParsedArtifact → EvidenceRef → Claim → PageProjection`。

模块与职责边界：

| 模块 | 职责 | 依赖 |
|---|---|---|
| `knowledge_types.py` | Claim/Origin/Relation/Scope 的数据契约与状态合法性 | 无 |
| `evidence.py` | 文本规范化、artifact 冻结、span 寻址与校验、`load_evidence` | `knowledge_types` |
| `chunking.py` | 结构分块；code point 坐标；evidence/context spans 与 render recipe | 无 |
| `claim_store.py` | v2 SQL、版本、原子提交、支持组、关系、REVIEW | `knowledge_types` |
| `projection.py` | 确定性页面模板、manifest、dirty、人工编辑检测 | `knowledge_types` |
| `retrieval.py` | scope 过滤、Page/Claim 双检索、去重、Why、source fallback | 上述全部 |
| `wiki_pipeline.py` | v2 prepare/extract/validate 编排，注入式模型角色 | `evidence`、`knowledge_types` |
| `wiki_prompts.py` | 按 discovery/synthesis/grounding/value/identity 定义契约与版本号 | 无 |

分发：v2 核心模块必须只用标准库，并复制到 `skills/lw/scripts/` 与
`plugins/llm-wiki/skills/lw/scripts/`。副本由 `scripts/sync_skill_distribution.py` 生成，
`tests/test_skill_distribution_parity.py` 守住字节一致。

跨布局导入用同一段 shim，使三份副本字节相同：

```python
try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .knowledge_types import Claim
except ImportError:
    from knowledge_types import Claim
```

## 阶段与验收边界

每个阶段是一个可独立验证的单元，验证通过才进入下一阶段。

| 阶段 | 交付 | 用例 |
|---|---|---|
| P0 | 契约文档、ADR、评测 manifest 骨架、基线报告 | 基线运行记录 |
| P1 | `knowledge_types.py`、`evidence.py`、chunker 寻址元数据 | E01–E08、M01–M02 |
| P2 | prompts + 纯提取 pipeline、dry-run 报告 | K01–K11、M03、X03 |
| P3 | `claim_store.py`、版本、原子提交、关系、支持组、REVIEW | R01–R08、V01–V03、T01–T06 |
| P4 | `projection.py`、`retrieval.py`、Why Chain | Q01–Q10 |
| P5 | Local/Shared/MCP/Web v2 接入、分发 parity | X01–X02、X04 |
| P6 | 迁移、备份恢复、回退 runbook | G01–G03 |
| P7 | 评测 harness、指标计算、发布报告 | 第 8.2 节指标分母分子 |

## 决策记录

`.scratch/knowledge-v2/decisions.tsv`，一行一个决策点。
