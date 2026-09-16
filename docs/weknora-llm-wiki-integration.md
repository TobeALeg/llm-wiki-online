# LLM Wiki 借鉴 WeKnora：源码映射与融合实施方案

日期：2026-09-16  
范围：小规模用户的文件解析、完整摄取、证据引用、增量合并与冲突处理。  
交付性质：源码审阅与实施设计；本次没有修改仓库，也未运行功能测试。

## 1. 实施结论与版本基线

保留现有 Python、SQLite、Markdown 页面及 MCP 入口，在现有 WikiCore 前增加文件解析和分块处理，在页面生成过程中增加证据关联与主题路由，在现有存储中补充来源版本映射和分块引用。

不引入 Redis、分布式任务队列、IM、企业级并发架构；首版串行处理即可。分块用于完整处理和定位证据，不要求同时建设向量数据库。

本文源码链接固定到审阅提交：

| 仓库 | 提交 |
| --- | --- |
| Tencent/WeKnora | `d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda` |
| TobeALeg/llm-wiki-online | `23d9f2d42beb3b7aaa370394db65eac14990e340` |

**事实与建议的边界：**“当前行为”来自固定提交的静态阅读；新增模块、接口、数据表及策略都是针对你的项目提出的设计，不能当作 WeKnora 原有实现或已完成改动。

在线仓库当前已经支持 `project_id`，`wiki_sources` 按项目保存不可变来源，`wiki_versions` 保存页面版本。后续新增关系也必须带项目范围。不要沿用“只有一个公司 Wiki”的旧假设。

## 2. 问题、参考代码、融合位置总表

| 问题 | WeKnora 参考 | 现有代码入口 | 建议 |
| --- | --- | --- | --- |
| 多格式文件不能直接进入核心 | W1、W2、W3 | U1 的文件扫描；U2 的 normalize_material | 核心外增加解析适配器，输出统一文档 |
| 长文件截断、未提交材料被记为处理完成 | W4、W5 | U1 的 source_bundle、apply_update | 按块处理，独立记录已扫描与已完成状态 |
| 引用只能定位整个来源 | W5、W6 | U2 的 validate_page；U3 的 commit_update | 增加块 ID、原文位置和页面引用 |
| 每次把已有页面全文交给模型 | W7、W6 | U4 的 submit | 先选候选主题，读取候选页全文，按页合并 |
| 新旧事实冲突和误合并 | W6、W7 | U2 的 organize；U3 的版本记录 | 明确冲突操作，不把“时间更晚”当成替代 |
| 长标识抄错、链接错误 | W8、W9 | U2 的输出校验；U1 的 lint | 短句柄映射、真实 ID 校验、链接检查 |

## 3. 源码导航

### WeKnora

- **W1：** [internal/types/docparser.go · ReadRequest / ReadResult / ParsedChunk](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/types/docparser.go)。统一解析结果、图片引用、分块正文与 ContextHeader。
- **W2：** [internal/infrastructure/docparser/doc.go](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/infrastructure/docparser/doc.go)。说明解析器与后续管线边界：不同解析引擎统一返回 ReadResult。
- **W3：** [docreader/parser/pdf_parser.py · _classify_page / _page_image_area_ratio / _extract_page_text](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/docreader/parser/pdf_parser.py)。按页区分扫描与文本；内置解析器把扫描页交给后续 OCR/VLM，不能把这个 Python 文件单独复制后就视为完整 OCR。
- **W4：** [internal/infrastructure/chunker/heading_splitter.go · splitByHeadingsImpl / findHeadingBoundaries / coalesceTinyChunks](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/infrastructure/chunker/heading_splitter.go)。按标题切分、维护标题路径、合并过小块，忽略代码围栏中的伪标题。
- **W5：** [internal/application/service/wiki_ingest_cite.go · extractCandidateSlugs / splitChunksIntoCitationBatches / classifyChunkCitations / resolveCitedChunks](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/application/service/wiki_ingest_cite.go)。候选主题抽取、分批关联原文块、解析引用。
- **W6：** [internal/agent/prompts_wiki.go · WikiCandidateSlugPrompt / WikiChunkCitationPrompt / WikiPageModifySystemPrompt / WikiDeduplicationPrompt](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/agent/prompts_wiki.go)。主题识别、引用、增量合并和去重规则。
- **W7：** [internal/application/service/wiki_ingest_dedup.go · selectDedupCandidatePages / exactIdentityTarget / dedupPairScore](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/application/service/wiki_ingest_dedup.go)。用身份与表面相似性筛候选，再判断是否同一事物。
- **W8：** [internal/application/service/wiki_slug_handles.go · handle / encodeContent / decodeContent](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/application/service/wiki_slug_handles.go)。模型使用短句柄，程序恢复真实标识。
- **W9：** [internal/application/service/wiki_lint.go · RunLint](https://github.com/Tencent/WeKnora/blob/d7ccd5ba517cc3968e6d5000a2ab0f4b51ef1dda/internal/application/service/wiki_lint.go)。参考页面关系的检查方式。

### 你的项目

- **U1：** [skills/lw/scripts/wiki.py · eligible_files / source_bundle / apply_update / do_update / lint](https://github.com/TobeALeg/llm-wiki-online/blob/23d9f2d42beb3b7aaa370394db65eac14990e340/skills/lw/scripts/wiki.py)。
- **U2：** [plugins/llm-wiki/llm_wiki_mcp/core.py · normalize_material / WikiCore.organize / validate_page / validate_update_package](https://github.com/TobeALeg/llm-wiki-online/blob/23d9f2d42beb3b7aaa370394db65eac14990e340/plugins/llm-wiki/llm_wiki_mcp/core.py)。
- **U3：** [plugins/llm-wiki/llm_wiki_mcp/store.py · SharedWikiStore.initialize / commit_update / restore_page](https://github.com/TobeALeg/llm-wiki-online/blob/23d9f2d42beb3b7aaa370394db65eac14990e340/plugins/llm-wiki/llm_wiki_mcp/store.py)。
- **U4：** [plugins/llm-wiki/llm_wiki_mcp/shared_service.py · SharedWikiService.submit](https://github.com/TobeALeg/llm-wiki-online/blob/23d9f2d42beb3b7aaa370394db65eac14990e340/plugins/llm-wiki/llm_wiki_mcp/shared_service.py)。
- **U5：** [plugins/llm-wiki/llm_wiki_mcp/remote_mcp.py · company_wiki_submit / local_wiki_organize](https://github.com/TobeALeg/llm-wiki-online/blob/23d9f2d42beb3b7aaa370394db65eac14990e340/plugins/llm-wiki/llm_wiki_mcp/remote_mcp.py)。

阅读顺序：U1 → W4 → U2 → W1/W3 → W5/W6 → U3/U4 → W7/W8。先理解数据如何进入，再研究如何生成和维护页面。

## 4. P0：修复完整摄取与完成状态

### 4.1 当前行为

U1 的 `eligible_files()` 跳过超过 128 KiB 的文件；`source_bundle()` 每个文件取前 24,000 字符，总文件内容预算为 180,000 字符。预算不足时截断或停止添加文件。

但 `apply_update()` 把全部 `records` 的哈希写回 `state["files"]`。这会让未提交给模型的文件、只提交了前半部分的文件，在下一轮可能被当作未变化。

这里“总预算”只针对该函数累加的文件片段，不能当作整个请求（包括 episodes 和已有页面）的总上下文保护。在线 U2 超限会拒绝输入，和本地 CLI 的截断行为不同。

### 4.2 怎么借鉴

W4 负责结构化切块，W5 将块分批处理。借鉴其分批思想，但**不要据此宣称 WeKnora 保证模型没有遗漏任何事实**；程序可验证的是块被处理过，事实是否完整仍需质量检查。

先做可独立交付的小修：

1. `source_bundle()` 额外返回每个文件实际提交的范围，以及 `complete / partial / deferred`。
2. 只更新完整处理成功的文件哈希；其余保持待处理，并在 `status` 显示。
3. 大文件跳过必须显示原因，不得无提示地消失。
4. 这只阻止“误记完成”，还不能让长文件自动完成；下一步必须引入分块。

随后增加：

- 按标题、段落切分；块太长再按句子、最终按字符边界拆分。
- 表格重复携带表头，代码块保留语言和围栏；不能为了不拆结构而绕过硬预算。
- 每块保留正文偏移与标题路径。标题路径单独存储，不插进原始正文改变偏移。
- 每次请求的预算包含提示词、候选目录、旧页、材料及预留输出。字符预算可作粗估，但不能当作准确 token 数。
- 短文件走同一条逻辑，只有一个块，避免维护两套行为。

### 4.3 状态与恢复

建议记录：

```json
{
  "source_id": "doc-123",
  "revision_id": "sha256:...",
  "parse_id": "sha256:...",
  "expected_chunks": 12,
  "completed_chunks": ["chunk-001", "chunk-002"],
  "state": "processing"
}
```

`revision_id` 表示原始来源内容版本；`parse_id` 由来源版本、解析器版本、解析选项和输出摘要确定。相同文件重新换解析器，不应复用失效的块定位。

只有所有块已成功处理、相关页面更新成功提交，才记为 `completed`。块无可提取知识也可以完成，但必须有明确的空结果；模型报错、无效 JSON 不算完成。

在线首版：中间抽取结果保存为准备状态，页面最后统一提交。失败可重用成功的块结果。若最终更新包超出预算，应拆成明确的多个提交批次，全部成功后再完成来源，不能静默丢弃后半。

本地首版：先校验全部生成结果，使用临时文件与待提交清单写回；完成状态最后更新。单个 JSON 原子替换并不能提供多页面事务，启动时需能重放未完成清单。

### 4.4 验收

- 30,000 字符文件的末尾唯一事实能进入处理材料，不只读取前 24,000 字符。
- 总量超过 180,000 字符的文件集能分批完成，未执行部分一直可见。
- 中途第 3 块失败后，重试不会漏块或重复生成页面。
- 已处理标记只在页面更新落地后生效。
- 修改解析配置后重新生成 parse_id，旧引用仍能回到旧解析快照。

## 5. P1：在 WikiCore 前增加统一解析接口

### 5.1 保留的边界

U2 的 `normalize_material()` 接收内容和来源信息，并拒绝路径、命令等字段。保留此边界：文件读取由 CLI 或可信适配器完成，WikiCore 继续负责知识组织和验证。

远程 `local_wiki_organize` 仍只处理客户端显式提交的内容；不要让远端任意读取调用者传来的服务器路径，也不要顺手改变其无持久化语义。

### 5.2 建议的统一文档模型

以下为新接口草案：

```python
@dataclass
class ParsedDocument:
    source_id: str            # 同一逻辑来源的稳定身份
    revision_id: str          # 原始内容版本
    parse_id: str             # 解析快照身份
    title: str
    markdown: str
    assets: list[AssetRef]
    locations: list[SourceLocation]
    parser_name: str
    parser_version: str
    warnings: list[str]

def parse_document(data: bytes, media_type: str, metadata: dict) -> ParsedDocument:
    ...
```

`locations` 保存规范化 Markdown 区间与原文位置的映射：PDF 页码、Word 标题或段落序号、聊天消息 ID。某解析器无法提供页码时明确标记 unavailable，不能从分块序号推算页码。

### 5.3 实现次序

| 格式 | 首版处理 | 检查重点 |
| --- | --- | --- |
| Markdown / TXT / 代码 | 本地解码，保留结构 | 编码错误、内容完整性 |
| Word | 使用现成文档解析库，统一输出 | 表头、合并单元格、段落顺序 |
| PDF 文本页 | 提取正文、布局与位置 | 双栏、页眉页脚、乱码 |
| PDF 扫描页 / 图片 | 接已有 OCR/VLM 能力 | 按页判断、失败可见、避免空文生成知识 |

W3 的文本页与扫描页判断值得参考，但它依赖项目其他部分完成 OCR。首版可以选择一个完整解析后端接入，不必复制其所有启发式，也无需支持全部格式才上线。

图片 OCR 是文字证据；VLM 对图表的解释需要标记为模型解释并保留原图，不应与直接提取的原文混为一类。

### 5.4 验收

一个包含文本页与扫描页的 PDF 能分别处理；双栏不交叉拼接；Word 表格不丢失表头关系；解析为空或部分失败时返回明确状态，不以文件名猜测知识。

## 6. P1：把来源引用细化到分块

### 6.1 当前基础

U2 校验页面 sources 是否属于允许集合；U3 已保存不可变 `wiki_sources`，相同项目和 source_id 的内容变化会被拒绝。这些机制应保留。

当前来源合法性不能证明某段正文由来源支持。W5 将主题关联到真实分块，W8 用短句柄降低模型抄错 ID 的概率。

注意：W6 的页面合并提示词要求正文不输出内部 chunk 句柄，引用关系由系统单独保存。WeKnora 的主题页—分块关联，不等同于精确到每个句子的证据证明。

### 6.2 你的最小实现

先做页面级块引用，不必一步做完整 claim 数据库：

```json
{
  "slug": "client-a-delivery",
  "sources": ["doc-123@revision-456"],
  "citations": [
    {
      "source_record_id": "doc-123@revision-456",
      "parse_id": "parse-789",
      "chunk_id": "chunk-003"
    }
  ]
}
```

这里 `source_record_id` 对应现有 `wiki_sources.source_id`，继续保持不可变；逻辑来源身份 `doc-123` 另存。不要把“同一文档的新版本”直接覆盖旧 source_id。

模型使用本次输入中的 `c001`、`p001` 短句柄，程序映射回真实 ID；真实 ID 由程序生成。未知句柄直接拒绝。

校验要同时检查：项目一致、来源记录存在、解析快照属于该来源、块存在、块确实提交给本次模型或属于允许保留的旧引用。

页面引用不代表逐句引用。将来需要点击具体结论时，再新增稳定 statement_id 与引用映射，不采用容易漂移的正文字符偏移作为结论身份。

### 6.3 回读能力

新增 `get_source_chunk(project_id, source_record_id, parse_id, chunk_id)`，返回原文、来源标题、位置和解析警告。页面展示引用卡片，MCP 可通过读取证据工具核对。

历史页面引用必须保存历史快照。新版页面改了引用后，查看旧版本和回滚仍能找到旧证据。

### 6.4 验收

伪造块 ID、其他项目块 ID 被拒绝；点击引用能读到同版本原文；回滚恢复正文与引用；旧页面没有块引用时仍可读，显示为来源级引用，不能伪造精确引用。

## 7. P2：按受影响主题生成更新

### 7.1 当前行为

U4 的 `submit()` 读取 `list_pages(project_id)`，把整个项目快照交给 `core.organize()`。U2 对已有页数设 500 上限（`MAX_EXISTING_PAGES`），对快照正文总量设 500,000 字符上限（`MAX_EXISTING_CHARS`）。即使用户很少，知识不断积累也会碰到此边界。

快照正文上限与单次请求的其余预算共同受模型窗口约束：材料 180,000 加快照 500,000 加返回包 240,000 必须小于 `MODEL_CONTEXT_TOKENS`（1,000,000，按中日韩字符约等于 token 估算）。窗口上限本身不解决“整库输入”带来的成本问题，只决定边界落在哪里。

输入侧另有两个与窗口无关的限制，它们才是“一份文档必须分批”的直接原因：单个材料正文上限 100,000 字符（`normalize_material` 未显式传入上限，继承了 `MAX_PAGE_BODY_CHARS`），材料合计上限 180,000 字符（`MAX_MATERIAL_CHARS`）。因此一份超过 180,000 字符的文档必须拆成多次提交；而在 180,000 以内可拆成多个材料一次提交。

### 7.2 借鉴与简化

借鉴 W5/W7 的候选识别与去重，但首版可把“每块抽取主题”和“关联该块证据”合成一次调用，减少轮次。长文不能只读取开头建立候选：每一批都允许发现新主题，最终合并候选。

建议的串行流程：

1. 解析并切块。
2. 每批提取主题候选及支撑块；输出引用原文，不反复摘要再摘要。
3. 使用标题、别名、类型和摘要找已有候选页。
4. 模型只判断歧义候选是否同一主题；不确定时保留候选关系，不强行合并。
5. 按最终 slug 汇总新证据。
6. 读取受影响页面全文与必要的旧来源，逐页合并。
7. 校验更新包，沿用现有提交与版本机制。
8. 用程序重建索引和链接。

候选召回初版可用 Python 字符串规范化、中文字符片段匹配和别名查找。不能仅依赖英文按空格分词，也不能直接复用仅为用户搜索设计的返回条数而假设召回完整。候选不足时扩展目录范围；超过预算分批，不取前 N 页后当作全部。

“相关”与“相同”分开：RAG 与向量数据库可以互相链接，但不能因常共现合成同一页。保留你现有 decision、process、guide 等页面类型，不照搬 WeKnora 仅为某阶段使用的实体／概念分类。

### 7.3 与当前代码融合

- U3 增加 `list_page_catalog()`（仅 slug、title、type、summary、aliases）和 `get_pages_by_slugs()`。
- U4 用新编排器替代整库 `core.organize()` 调用；继续按同一 project_id 读取与提交。
- U2 保留验证能力，把单次组织扩展为可测试的“抽取、匹配、合并”步骤。
- 显式传入外部 update 包的路径也必须走相同证据校验，不能因为跳过模型就跳过验证。
- 模型请求预算限制和业务级提交包限制分离：减少模型上下文不意味着可以把超大提交包直接塞进现有 normalize_materials。

### 7.4 验收

新增客户 A 纪要只读取候选页全文；同一主题换简称不新建重复页；相关但不同概念不误合并；超过 500 页时能完成候选检索；同一批次多个块提及同一主题只产生一个合并结果。

### 7.5 决策（已确定）

由需求方在评审中明确，作为 P2 实现的约束。

**D1 接受两次模型调用。** 第一阶段送页面目录选出受影响主题，第二阶段只送这些页面的全文与本次材料做逐页合并。不以延迟为由退回整库输入。

**D2 受影响页面由模型从目录中选出，客户端不预先指定。** 需求方提交时通常不知道该改动哪些页面，因此不采用“客户端传 affected_slugs”的方案。7.2 第 3、4 步的候选召回与歧义判定必须留在服务端完成。

**D3 “忽视”是不送不动。** 被忽视的范围既不进入模型输入，也不允许被本次提交修改。这是过滤器语义，不是预算语义；不要实现成“可以改但不发送”的范围限定。

选择这一方向的量化依据：每页标题 30、摘要 150、正文 3,000 字符的剖面下，纯目录约为全量快照字符数的十二分之一（按 `normalize_existing_pages` 与 `normalize_catalog` 之后的 JSON 长度计：20 页 61,331 → 5,081；50 页 153,341 → 12,701）。这使每次提交的输入规模与“本次改了什么”相关，而不再与“Wiki 总量”相关。

该比例对正文长度敏感，所以它是尺寸触发的依据，不是页数触发的依据。同一剖面把正文压到 100 字符时，目录反而是快照的 1.53 倍，即路由更贵；原因是目录每条记录的身份开销有下限而正文没有。

### 7.6 依赖与顺序

先纠正一个容易误判的点：**P0 与 P1 对 P2 都不是硬前置。** 两阶段的最小形态（阶段一送材料加目录选出受影响 slug，阶段二送这些页面全文与材料做合并）只需要 P2 自身的新代码，不需要服务端分块，也不需要解析适配器或块级引用。

理由是可核对的：服务端提交路径不会静默截断，它直接拒绝。`normalize_materials` 与 `normalize_existing_pages` 都是超限即抛 `CoreError`（`core.py:85`、`core.py:113`）。P0 要修的“未提交材料被记为处理完成”是本地 CLI 文件扫描的缺陷，服务端从来不存在。而材料本身的上限（单个 100,000、合计 180,000 字符）已经保证一次调用的输入可控，客户端按此拆分即可，服务端无需再切块。

因此依赖分三类，**没有硬依赖**：

**P2 自身新增（真正的代价所在）：**

- `list_page_catalog()` 与 `get_pages_by_slugs()` 当前都不存在（`store.py` 只有 `list_pages`，返回含正文的完整页面）。
- 两阶段编排：`SharedWikiService.submit` 现在是单次 `core.organize`（`shared_service.py:62`），要拆成“抽候选、定歧义、取正文、逐页合并”的可测步骤。
- `aliases` 当前不存在。页面表只有 slug、title、type、status、tags、summary、body、sources（`store.py:126-139`）。§9 将其列为页面与版本元数据扩展，§11 阶段 E 的验收点名别名，故属于 P2 范围。

**条件依赖（只在做更深一层时才需要）：**

- **阶段 A/B 的分块与打包**：只有当 P2 要在服务端做块级主题抽取、而不是以材料为单位路由时才需要。届时需按 §10 把纯分块与验证代码做成服务端可安装的共享模块；`chunking.py` 目前只在 `skills/lw/scripts/` 下，`import llm_wiki_mcp.chunking` 与 `import chunking` 均失败。以材料为粒度路由可以完全绕开这一点。
- **解析适配器（§5，阶段 C）**：只在支持 PDF/Word 等非文本格式时需要；TXT/Markdown 不需要。
- **块级引用（§6，阶段 D）**：只在要求引用落到块粒度时需要；现有 source 级引用已可用，P2 可先按来源级引用上线。

### 7.7 与 P0/P1 的关系

P0 与 P1 排在前面的原因不是 P2 依赖它们，而是它们各自解决不同性质的问题：

- **P0 解决正确性。** 它修的是“内容被静默丢弃却记为完成”，属数据损坏，且不可见。正确性缺陷优先于成本优化，因为成本问题只是慢，数据损坏是错。
- **P1 解决可追溯。** 它让结论能回到原文（§6 回读），以及让非文本格式可摄入（§5）。这是信任问题，不是效率问题。

**P2 解决成本与规模。** 它让单次提交的输入从“与 Wiki 总量成正比”变成“与本次改动相关”，并消除 500,000 字符快照上限带来的增长天花板。系统在 P2 之前是可用的，只是越用越贵、越用越接近边界；这正是它排在 P0/P1 之后、却仍然值得做的原因。

### 7.8 开放问题

D3 与 7.4 的“同一主题换简称不新建重复页”存在张力。若被忽视的页面完全不出现在目录中，模型无从知道它存在，就可能为同一主题新建一个重复页面。

建议的解法是把“不送正文”与“不出现在目录”分开：被忽视的页面仍以 slug 与 title 出现在目录中并标记为不可修改，正文永不发送，也永不被更新。这样 D3 的两条都能满足，同时保留防重复能力。此项需需求方确认后再实现。

## 8. P2：显式处理冲突、替代与人工修订

参考 W6 的 `WikiPageModifySystemPrompt`：新增事实需要新证据支持；明确替代时更新并解释；冲突未决时保留旧内容并记录冲突；个人／宣传自述需要保留归属。

你的输出协议建议增加：

```json
{
  "operation": "record_conflict",
  "slug": "delivery-plan",
  "reason": "新纪要提出备选方案，但没有确认替换",
  "evidence": ["c003"],
  "proposed_status": "current"
}
```

允许操作：`create / append / revise / supersede / record_conflict / no_change`。由程序检查合法值，实际正文仍通过完整校验后提交。

- 较晚的导入时间不等于较晚的事实生效时间。
- “计划使用 B”不能自动使“当前采用 A”失效。
- 源文删除不证明事实为假；先标记支持证据已撤回或需复核。
- 人工修改来源、时间、备注应进入页面版本元数据。没有证据支持时，自动更新不得抹去人工判断。
- 回滚创建新版本，沿用 U3 现有方式；同时恢复该版本引用和冲突状态。
- `no_change` 可以完成某块处理，但不必额外制造相同页面版本。

建议保留 `issues_json` 和 `edit_source` 元数据即可，首版不建设复杂问题工单系统。业务冲突与 SQLite 的 base_version 过期是两类错误，响应字段要区分。

验收：未决方案不覆盖当前方案；明确批准的新方案可以替代并保留历史；新导入旧材料不会自动覆盖新决策；恢复旧版本后正文和引用一致。

## 9. 推荐的最小存储扩展

全部为建议的新结构；字段名可在实现时统一，但语义不应省略。

| 结构 | 关键字段／关系 | 用途 |
| --- | --- | --- |
| 现有 wiki_sources | project_id + source_id，不可变 content | 保存某一来源版本的正文 |
| 新 wiki_source_revisions | project_id、logical_source_id、revision_id、source_record_id、supersedes | 将同一来源多个版本串起来 |
| 新 wiki_parses | project_id、parse_id、source_record_id、parser_version、options_hash、markdown、locations_json | 保持解析快照与定位一致 |
| 新 wiki_chunks | project_id、parse_id、chunk_id、start、end、heading_path、content | 分块与原文位置 |
| 新 wiki_page_citations | project_id、slug、page_version、parse_id、chunk_id | 当前页与历史页引用 |
| 新 wiki_ingest_runs | project_id、run_id、input_digest、purpose_hash、state、expected_chunks | 恢复与完成判定 |
| 新 wiki_ingest_results | project_id、run_id、chunk_id、result_json、state | 缓存成功抽取，失败可重试 |
| 扩展页面和版本元数据 | aliases、issues_json、edit_source | 主题身份、冲突和人工修订 |

每个复合引用必须带 project_id。页面版本引用应使用实际已提交版本号，不能使用模型返回的版本号。

`commit_update()` 目前会重新调用 `normalize_materials()`，因此仅在模型调用之前拆块还不够。新增内部 `commit_prepared_update()`，按 run_id 读取已校验、属于该项目的准备材料，再将来源、页面、引用、版本与完成状态在同一事务提交；禁止模型或客户端任意指定其他准备任务。旧 `commit_update()` 作为小文本兼容入口保留。

二进制原件如需保存，用本地文件目录＋数据库引用即可；保存内容摘要，解析失败也保留错误信息。不要为此引入对象存储系统。

## 10. 模块与接口改动清单

新增模块均建议放在 `plugins/llm-wiki/llm_wiki_mcp/` 下：

| 文件 | 改动 |
| --- | --- |
| 新 document_ingest.py | 定义解析结果，调用解析适配器 |
| 新 chunking.py | 结构切分、上下文和预算 |
| 新 evidence.py | 来源版本、块 ID、短句柄、引用验证 |
| 新 wiki_pipeline.py | 串行抽取、路由、逐页合并、准备提交 |
| 新 wiki_prompts.py | 独立管理抽取、去重、冲突规则 |
| core.py | 协议 v2；保留 v1 小文本兼容；验证新增元数据 |
| shared_service.py | 将提交接到编排器 |
| store.py | 迁移、准备材料、引用、候选目录和提交 |
| remote_mcp.py | 保留现有工具；增加证据读取与处理状态 |
| skills/lw/scripts/wiki.py | 修复完成标记，接入分块，扩展 status/lint |
| plugins/llm-wiki/skills/lw/scripts/wiki.py | 检查对应分发副本，确保修复同步 |

本地 CLI 当前为独立脚本，不能简单 import 只有服务器安装时才存在的包。实施时为共享的纯解析／分块／验证代码增加可安装包或明确打包步骤，保留 `/lw` 调用方式；上线前检查两份脚本的分发关系。不要为了这次改动另造一套本地与在线的算法。

不新增任意远程文件路径参数。首版可由客户端解析后提交规范化文档；若以后支持服务器收文件，应使用独立上传入口取得受控 upload_id，再交给解析器。无需为了完善核心处理先制作上传 UI。

### 协议迁移

- v1 materials 和 update package 继续接受，标记为来源级引用。
- v2 增加 source revision、parse、chunk 与 citations，不静默丢弃这些字段。
- 现有 `normalize_material()` 只返回固定字段，必须显式更新，否则新增元数据会被剥离。
- `validate_update_package()`、哈希计算、序列化、版本保存、读取与回滚共同升级。
- source/chunk/citation 的规范化排序要稳定，保持重复提交语义。
- 旧来源可延迟切块；旧页面只有经过重新核对后才增加块引用。
- 当前基础版本检查、不可变来源规则、项目隔离继续复用，不增加新的并发框架。

## 11. 分阶段交付与测试

| 阶段 | 可交付结果 | 必须验证 |
| --- | --- | --- |
| A | 修复 CLI 误记完成 | 截断、预算耗尽、失败重试 |
| B | 文本结构分块与恢复 | 末尾材料覆盖、位置一致、无重复提交 |
| C | 文档解析适配器 | 混合 PDF、Word 表格、解析失败 |
| D | 段落引用与回读 | 同版本定位、跨项目拒绝、历史恢复 |
| E | 主题路由与增量合并 | 候选召回、别名、相关与相同 |
| F | 冲突规则和旧数据升级 | 未决不覆盖、明确替代、v1 兼容 |

测试分两类：

1. 确定性测试：覆盖状态、ID、原文位置、迁移与事务；模型用固定输出替身，避免把 API 波动混入程序正确性。
2. 小型真实材料评估：长文末尾关键信息、同义主题、未决冲突和表格阅读；检查原文与生成结果，不只检查 JSON 合法。

建议的关键样例：

- 长文中只在末尾出现的交付日期。
- 同一客户的中文名与英文简称。
- “提出新方案”与“正式批准新方案”两份材料。
- 两个项目包含同名页面和相同局部 chunk 编号。
- 旧页面回滚时所引来源已有新版本。
- 扫描页有低质量隐藏文字层。
- 一次来源处理结束但没有可沉淀知识的有效空结果。

验收报告分别记录：程序覆盖率（全部块是否处理）、提取质量（事实是否保留）、引用质量（证据是否支持）、更新质量（是否错误覆盖）。这四项不能相互替代。

## 12. 第一批开发任务

1. 为 source_bundle 与 apply_update 的截断完成问题添加回归样例并修复。
2. 抽出结构分块与处理清单；保证长文本可以完整串行跑完。
3. 扩展协议和来源版本／解析快照／块引用，先打通 TXT/Markdown。
4. 在已有 SharedWikiStore 上实现原文回读和历史引用。
5. 接入所需文档解析器。
6. 用候选主题路由替换整库全文输入。
7. 增加显式冲突规则及回滚兼容验证。

第一批不要把全部 WeKnora 流程搬进来。先做到：**长文不漏处理，页面能回到原文，更新只触及相关知识，未决信息不会覆盖已确认决策。**

