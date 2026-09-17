# ADR 0001：Claim 为权威状态，Page 只做投影

状态：已采纳

## 背景

v1 把模型生成的 Markdown 页面直接写入 `wiki_pages`，页面正文就是知识。这带来三个具体后果。

页面合并时模型重写整页文字，读者无法分辨哪句来自材料、哪句是模型的连贯性补写。同一命题
在两个页面里各写一次，撤回一处不会影响另一处。历史页面可以整个恢复，包括它记录的当时
结论，而从没有任何东西表示那个结论后来变了。

## 决策

引入 Claim 层作为权威状态。链路是 `SourceRevision → ParsedArtifact → EvidenceRef → Claim
→ PageProjection`。页面由一组已提交的 claim 版本按固定模板渲染，manifest 记录用了哪些版本。

写入分两层保护：

- `SharedWikiService.submit` 在 v2 开启时拒绝预先构造的 `pages[]`，错误信息指向上报材料的
  `ingest`。直接提交页面包会让没有 claim 支撑的文字进入 Wiki。
- `SharedWikiService.restore` 在 v2 开启时拒绝直接恢复历史页面，并指向
  `restore_as_manual_note`。恢复页面文字会让被替代的措辞回到页面上，而替换它的 claim 不动，
  于是页面与知识不一致，且页面会被先读到而胜出。

## 后果

页面可以删除并重新生成，删除页面不改变知识。

投影失败不回滚知识，返回 `committed_projection_pending`，查询时从当前 claim 生成保守模板。

v1 的页面读接口继续工作，v1 数据保留。旧客户端在 v2 开启后得到明确的拒绝，而不是静默的
不同行为。

用户手改生成的 Markdown 时先检测差异，把新增知识转成 `manual_note` source 重新摄取，
不静默覆盖。

测试：`tests/test_modes_and_adapters.py`（X04）、`tests/test_projection_retrieval.py`（Q01–Q03、
Q08、Q09）。
