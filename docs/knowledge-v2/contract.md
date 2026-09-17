# LLMWiki Knowledge Architecture v2 契约

状态：已实现，随代码一起进版本。本文是 v2 的语义契约；每一项都指向守住它的模块或测试。

术语按链路顺序：`SourceRevision → ParsedArtifact → EvidenceRef → Claim → PageProjection`。

## 1. 权威状态在哪

Claim 是权威知识。Page 是投影，可以被删除并重新生成，删除页面不改变知识。

| 对象 | 存放位置 | 不可变？ |
|---|---|---|
| Source / SourceRevision | `sources`, `source_revisions` | 修订不可变；同源新内容是新修订 |
| ParsedArtifact | `parsed_artifacts` | 不可变，旧引用不随新 parser 漂移 |
| EvidenceRef | `evidence_refs` | 锁定 artifact 与 span，可校验 |
| Claim / ClaimVersion | `claims`, `claim_versions` | 版本不可变；当前指针由事务更新 |
| ClaimOrigin | `claim_origins`, `claim_evidence` | 每次来源归因各存一条，不覆盖 |
| PageProjection | `page_projections`, `page_claims` | 可重建，manifest 记录来源版本 |

实现见 `plugins/llm-wiki/llm_wiki_mcp/claim_store.py`。

## 2. 坐标与寻址

`offset_unit = unicode_code_point`，区间为半开 `[start, end)`，偏移基于**已保存的
normalized_text**。Python 字符串按 code point 索引，因此 `text[start:end]` 就是全部换算；
本实现不出现 UTF-8 或 UTF-16 长度。

规范化只做一件事，把 CRLF 和 CR 统一成 LF。组合字符、emoji、全角形式原样保留，因为
折叠它们会让恢复出的引文与读者能去核对的原件不一致。

规范化文本的 UTF-8 字节用于 hash，不用于偏移。

校验时失败而不是裁剪：span 越界、artifact 文本 hash 不符、span hash 不符都抛错，
不返回较短的"看起来像原文"的片段。错误码区分 `EVIDENCE_NOT_FOUND`、`SCOPE_MISMATCH`、
`HASH_MISMATCH`、`SOURCE_WITHDRAWN`、`RAW_UNAVAILABLE`、`INVALID_SPAN`、`STRUCTURE_MISMATCH`。

客户端只提交规范化文本时 `raw_available = false`，服务端不声称持有从未收到的原件。

实现见 `evidence.py`、`chunking.py`。测试 `tests/test_evidence_addressing.py` 覆盖 E01–E08。

## 3. 重建文本不是原文

分块器对超出预算的表格和代码块会重复表头或在块外补围栏。这三样被分开记录：

- `evidence_spans`：artifact 中真实存在的范围，引用只能落在这里。
- `context_spans`：渲染时重新发出的真实范围，例如表头行、分隔行、真实的围栏行。
- `render_notes`：不是任何真实范围的补造物，例如 `fence_closed_synthesized`。

`render_recipe` 取值 `verbatim`、`table_with_header`、`code_with_fence`、
`character_window`。非 verbatim 的块的 `verbatim` 为 false。因此补造的围栏不会出现在
任何可引用范围里，也不会被当成原文恢复。

表格证据要能同时恢复列名、相关行、单位与脚注。表头与分隔行存为 `context_spans`，表格
下方连续的脚注行记入 `footnote_lines`，两者都由冻结的 `structure` 提供，恢复时不需要
重新运行 parser。

## 4. Claim 的粒度

Claim 是最小可独立理解、引用、维护和判断是否仍适用的知识单元，不是最短句子。

会改变含义的项目、对象、条件、否定、量词、时间范围和表达强度必须保留。把"仅在当前低
数据量场景使用 SQLite"拆成无条件的"使用 SQLite"是禁止的。

流程知识保存为有序的 `attributes.steps[]`，每步带输入、输出、前提、例外与依赖顺序。
只有步骤具有独立维护需求时才额外建子 claim。

`attributes` 按 `knowledge_kind` 校验：`process` 只接受 `steps`，`decision` 只接受
`adopted_by`，`architecture` 只接受 `components`，其他 kind 不接受任何键。

## 5. 状态轴是正交的

| 字段 | 取值 | 谁使用 |
|---|---|---|
| `knowledge_kind` | fact / definition / distinction / judgment / decision / rationale / constraint / process / architecture / open_question | 全部 |
| `derivation` | explicit / synthesized | 全部 |
| `epistemic_status` | asserted / hypothesis / verified / disputed / not_applicable | 全部 |
| `lifecycle_status` | active / superseded / retracted | 全部 |
| `grounding_status` | grounded / needs_revalidation / unsupported | 全部 |
| `decision_state` | proposed / adopted / rejected / null | 仅 decision |
| `question_state` | open / resolved / deferred / null | 仅 open_question |

`asserted` 表示材料表达了这个命题，不表示命题为真。`grounded` 表示当前文字与归因忠于
证据，不表示外部事实核验完成。

`decision_state` 出现在非 decision 上、`question_state` 出现在非 open_question 上，都是
错误，不是可以忽略的字段。

### 5.1 需要证据的状态

`ClaimState.required_support()` 是唯一回答"这个状态需要什么才能存"的地方：

| 状态 | 需要 |
|---|---|
| `epistemic_status = verified` | `verification_record` |
| `epistemic_status = disputed` | `dispute_record` |
| `decision_state = adopted` | `adoption_record` |
| `question_state = resolved` | `resolution_record` |

缺少时**拒绝**，不静默降级。降级会掩盖模型试图越权这件事本身。想要 `proposed` 的调用者
必须写 `proposed`。

零容忍项由这条规则直接守住：凭空生成的 adopted decision、把假设写成已验证事实，都在
`validate_claim_state` 处失败。

## 6. 身份、版本与来源

| 变化 | 行为 |
|---|---|
| 同义表述、纯编辑修正 | 同 `claim_id`，新版本 |
| 新证据支持完全相同的命题和适用范围 | 同 `claim_id`，追加 origin 与支持组，新版本 |
| 完全相同的重复导入 | `status = noop`，不新增版本，知识版本号不前进 |
| 改变条件、结论、主体或适用范围 | 新 `claim_id`，按证据建 `refines` 或 `supersedes` |
| 单纯后来又讨论同一主题 | 不自动建 `supersedes` |
| 原结论被明确撤回 | 新版本标 `retracted`，保留撤回来源 |

命题未变而证据增加时，上一版本的 origins 会被带到新版本，支持组 id 也保留。这样两个独立
来源就是同一个版本上的两个独立支持组，撤回其中一个不会让 claim 失去支持。命题改变时
不带走任何 origin，因为旧 grounding 不得被新措辞复用。

同一个 claim 先由系统综合得到、后来又被用户明确说出时，新增一个 `explicit` origin，
旧的 `synthesized` origin 原样保留。`derivation` 不会从 synthesized 改成 explicit。

## 7. 关系的方向

| Relation | 固定方向 |
|---|---|
| `supports` | 前提 Claim → 被支持 Claim |
| `derived_from` | 推导 Claim → 前提 Claim |
| `refines` | 更具体的新 Claim → 原 Claim |
| `supersedes` | 替代者 → 被替代者 |
| `contradicts` | 语义对称，按端点排序去重 |
| `depends_on` | 依赖方 → 被依赖方 |

方向由 `knowledge_types.derive_from_direction` 具名，不让读者从关系名反推。

`derived_from` 必须锁定前提的**版本**，且不得成环。`claim_relations` 只存 Claim→Claim；
Topic 演进图另行建模，不让 `topic_id` 混进无类型的端点列。

`refines` 不自动让原 claim 失效。`supersedes` 需要明确采用或替换依据；有歧义时进 REVIEW。
模型推断的冲突不等于已裁决的冲突，记录 `contradicts` 不改变任何一端的 epistemic_status。

## 8. 支持组

```
支持组 G1：A AND B，共同支持 C
支持组 G2：D，独立支持 C
G1 OR G2：任一完整有效的支持组可维持 C 的支持状态
```

组内是 AND，组间是 OR。实现只做这一层，不引入规则引擎。

`support_group_outcome` 的三个结果：整组可用为 `grounded`；部分可用为
`needs_revalidation`；全不可用为 `unsupported`。

来源撤回时重新计算可用性，而不是删除 claim 或断言其为假。可用性每次都从 evidence 与
premise 行本身读出，不信任缓存列，并且循环到不动点，因为失去支持的 premise 不再是可用的
premise。

## 9. REVIEW

REVIEW 用于有语义后果的歧义，不用于普通"尚未验证的假设"。

每个 REVIEW 记录问题、候选、相关原文、影响范围、触发原因与主题对象的当前版本。

动作与含义是分开的：

| 动作 | 改变什么 | 不改变什么 |
|---|---|---|
| `retain` | 记录长期保留意图 | epistemic_status、decision_state、derivation 全不变 |
| `edit` | 产生 manual_note 待处理 | 不直接覆盖 claim |
| `reject` | 记录不保留 | 不删除已提交历史 |
| `adopt_decision` | 新版本 `decision_state = adopted`，记录 adopted_by | 不改变 derivation |
| `confirm_supersession` | 建 accepted 的 supersedes 边，被替代方转 superseded | 不改写被替代方的 statement |
| `confirm_identity` | 把 claim 关联到选定 topic | 不合并其他候选 |

`retain` 不是 `adopt_decision`。点击"保留这个假设"只改变保留意图。

审核引用的版本已经变化时返回 `StaleReviewError` 并带上当前版本，审核保持 open。
旧审核不能覆盖新知识。

## 10. 事务与失败语义

提交是一个事务，写入 claim 与版本、origin、evidence 连接、支持需求、关系、topic、审核队列、
审计、幂等结果、知识版本号与投影 dirty 标记。最终 ID 与时间戳由 Store 分配，不接受模型
返回值。

run 状态机只接受合法迁移：

```
received → preparing → extracting → validating → ready_to_commit → committed → projecting → completed
                                                                 ↘ committed_projection_pending
任一行进状态 → failed
```

`ready_to_commit` 之前失败、批次未完成的 run 不会读成 `completed`。空材料、模型失败、
零个有价值候选是三种不同结果。

投影失败不撤销已提交知识，返回 `committed_projection_pending`，查询时从当前 claim 生成
保守模板。知识版本号只在写了知识时前进：pending review 和 recorded drop 是候选的记账，
不是知识。

## 11. 幂等与内容指纹

- 同 scope、同 key、同请求 → 返回相同结果，不新增版本或重复边。
- 同 scope、同 key、不同请求 → `IdempotencyError`，不返回旧结果冒充新成功。
- 同 source revision 重复导入 → 修订复用，提交为 `noop`。
- 同内容不同来源 → 内容指纹识别，同一 claim 追加 provenance。
- `base_version` 过时 → `ConflictError` 带当前版本，不写入。
- 阶段缓存以输入 hash、配置、prompt 版本与模型指纹为键，任一变化都不命中。

## 12. 模型边界

模型调用不写 Store。模型输出先变成 Candidate，再经过最终验证，再由 Store 分配身份。
`artifact_extractor` 与 `validate_changes` 共用同一套校验，所以不存在一条"另一条更宽松的
路径"。

材料正文里的指令是材料，不是指令。`screen_material` 标出可疑片段并记录，但不执行、不因它
改变任何状态，也不因它抛错。

## 13. 未实现与明确的边界

- 没有 Neo4j、分布式队列、多 Agent 编排、学习排序、图传播 embedding。
- 只有 SQLite 一套实现。
- 不做全网事实核查，不承诺识别所有推理错误。
- 不用模型自报 confidence 或价值分控制发布。
- 外部服务端不接收文件系统路径参数。
- 跨知识空间不自动同步；复制需要显式导入并重新映射。

## 14. 相关文档

- 架构与模式边界：`docs/architecture.md`
- 决策记录：`docs/adr/`
- 验收用例与映射报告：`evals/knowledge_v2/acceptance_cases.json`、`case_map.json`
- 评测口径与指标：`evals/knowledge_v2/manifest.json`
