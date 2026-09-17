# ADR 0002：证据按 Unicode code point 寻址，重建格式不冒充原文

状态：已采纳

## 背景

分块器对超长表格和代码块会重建内容：每片重复表头与分隔行，代码块在外面补围栏。这些重建
文本是给模型读的，不是原文。v1 的注释把这些偏移写成字节，而实现用的是字符串切片，注释与
行为不一致。

不区分的话有三类错误会同时出现：引用按 UTF-8 字节长度算偏移导致中文与 emoji 错位；补造的
围栏被当作真实原文引用；表格被拆分后结论依赖的列名和单位无法恢复。

## 决策

偏移单位固定为 Unicode code point，区间半开 `[start, end)`，基于已保存的 normalized_text。
规范化只统一行尾，不折叠组合字符、emoji 或全角形式，折叠会让恢复出的引文与原件不一致。
规范化文本的 UTF-8 字节只用于 hash。

每块分三样记录：

- `evidence_spans` 是可引用范围，只有 artifact 里真实存在的文本能进去。
- `context_spans` 是渲染时重新发出的真实范围，例如表头行与真实围栏行。
- `render_notes` 记录不属于任何真实范围的补造物，例如 `fence_closed_synthesized`。

`render_recipe` 具名渲染方式，非 verbatim 的块 `verbatim` 为 false。

表格的列名、单位与脚注从冻结在 artifact 里的 `structure` 恢复，不重新运行 parser。这样旧
parser 不在机器上时旧引用仍能解析。

## 后果

恢复不到就报错，不裁剪。span 越界、artifact hash 不符、span hash 不符各自有错误码，不返回
较短的替代片段，也不调用模型补一段"看起来像原文"的内容。

旧 parser 缺失但 artifact 完整时证据仍可读；artifact 丢失时明确失败。

解析输出不可变。同一 source 有新内容是新修订，parser 或 config 升级是新 artifact，旧引用
继续读旧快照。

实现：`evidence.py`、`chunking.py`。测试：`tests/test_evidence_addressing.py`（E01–E08）、
`tests/test_lw_chunking.py`。
