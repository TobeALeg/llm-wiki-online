"""Append the run's decision rows to the canonical trail."""

from pathlib import Path

TRAIL = Path(__file__).with_name("decisions.tsv")

ROWS = [
    ("frame", "范围定为两份 v2 规格的完整实现，含 P0–P7，但真实模型 holdout 与人工 gold 确认列为未执行",
     "这两项需要 API key 和人的判断，机器签收会变成伪造的通过依据",
     ".scratch/knowledge-v2/plan.md", "范围已定，未执行项如实标注"),
    ("frame", "基线实测记 175 个用例 173 通过 2 失败，不引用提交说明",
     "后面每次检查都要读成旧值对新值，基线的来源必须是自己跑的",
     "reports/test-baseline.md", "2 个失败各有根因，一个是缺 mcp 依赖，一个是测试自身泄漏"),
    ("P1", "偏移单位钉成 unicode code point，区间半开，规范化只统一行尾",
     "按字节或 UTF-16 算偏移会让中文与 emoji 恢复出错位文本，折叠组合字符会让引文与原件不一致",
     "docs/adr/0002-evidence-is-addressed-by-code-point.md", "E01–E08 全部通过"),
    ("P1", "分块的 evidence_spans、context_spans、render_notes 三样分开记，补造的围栏不进任何可引用范围",
     "重建格式被当成原文引用是伪造引用，属于零容忍项",
     "plugins/llm-wiki/llm_wiki_mcp/chunking.py", "E03、E04 通过"),
    ("P1", "v2 核心模块只用标准库，分发副本由脚本生成而不是手工同步",
     "干净安装的机器上没有服务器包，手工同步三份必然漂移",
     "docs/adr/0003-vendored-core-is-stdlib-only.md", "parity 测试守住字节一致与依赖边界"),
    ("lever", "先建 56 项用例的映射测试，用例 ID 由测试自己声明，报告由测试自己写",
     "「全部用例通过」只有在每条都能失败时才有意义，编辑测试文件改不动期望集合",
     "tests/test_acceptance_case_map.py", "56/56 覆盖，无未知 ID"),
    ("P3", "提交是一个事务，ID 与时间戳由 Store 分配，不接受模型返回值",
     "模型回显的 ID 一旦被采信就变成伪造的权威版本",
     "plugins/llm-wiki/llm_wiki_mcp/claim_store.py", "M03、T01–T06 通过"),
    ("P3", "支持可用性每次从 evidence 与 premise 行本身读出并循环到不动点，不信任缓存列",
     "缓存列会让来源撤回后状态不变，R07 一开始就是这样假绿的",
     "claim_store.py::_recompute_support", "撤回一个独立支持组后仍 grounded，全撤回后 unsupported"),
    ("P3", "命题未变而证据增加时把上一版 origin 带到新版本；命题改变时不带",
     "两个独立来源要是同一个版本上的两个支持组，而旧 grounding 不得被新措辞复用",
     "claim_store.py::_carried_origins", "R03、R07 通过"),
    ("P5", "v2 用例写成 knowledge_service 一份，Local 与 Shared 只在数据库文件与 actor 上不同",
     "两份实现是两种模式悄悄不再一致的方式",
     "plugins/llm-wiki/llm_wiki_mcp/knowledge_service.py", "X01 用受控提取器断言两边语义 ChangeSet 相同"),
    ("P5", "开启 v2 后拒绝 pages[] 写入与直接恢复历史页面，历史措辞改走 restore_as_manual_note",
     "直接写页面会让没有 claim 支撑的文字进入 Wiki，恢复页面文字会让被替代的措辞先被读到而胜出",
     "docs/adr/0001-claim-is-authoritative-page-is-projection.md", "X04 通过"),
    ("P5", "本地 /lw 增加 knowledge-prepare 与 --candidates 的离线提取路径",
     "驱动 /lw 的 agent 本身就是模型，这条路不需要 API key 也能完成一次真实摄取",
     "skills/lw/scripts/wiki.py", "X02 在 llm_wiki_mcp 不可导入的子进程里通过"),
    ("audit", "发现 id 宽度契约自相矛盾：内容摘要生成 64 位而 pattern 只收 32 位",
     "自己铸造的地址无法被自己校验引用，子代理为此加了一层截断别名绕开它",
     "knowledge_types.py 的 _DIGEST", "pattern 接受两种宽度，流水线里的绕行代码删除"),
    ("audit", "发现幂等键在 base_version 前进后把重试判成不同请求，崩溃恢复会永久卡住",
     "迁移子代理实测复现：重跑永远抛 IdempotencyError，且重试不会有进展",
     "claim_store.py 的 intent_hash", "同意图重试取回已提交结果，不同意图仍拒绝，不新增版本"),
    ("audit", "发现 review_decisions 缺 request_hash 列，任何审核动作都不可达",
     "V01–V03 三个用例直接抛 OperationalError，说明审核路径此前从未真正跑过",
     "claim_store.py 建表与 INSERT", "补列并加 _add_missing_columns，V01–V03 通过"),
    ("audit", "发现未知顶层字段的 ChangeSet 被部分写入，v1 的 pages[] 可以夹带进来",
     "绕过 claim 层的写入正是规格禁止的那一条，部分应用比整体拒绝更危险",
     "claim_store.py 的 CHANGE_SET_FIELDS 白名单", "M03 形状断言通过，v1 载荷整体被拒"),
    ("verify", "渲染器接到用例层，新页面总是排队，已存在页面只在受影响时重建",
     "接上之前 ingest 报 completed 却没有任何页面，属实测发现的断路",
     "knowledge_service.py::projection_renderer", "实测页面落到磁盘，manifest 指向 claim 版本"),
    ("verify", "删掉子代理为绕开 id 宽度而写的测试断言，改成断言引用与注册地址相同",
     "该断言与它自己的测试名矛盾，留着就是把绕行固化成契约",
     "tests/test_knowledge_extraction.py", "34 个用例仍全绿"),
    ("verify", "修掉基线里唯一的未解释失败：测试自己泄漏 sqlite 连接",
     "验收清单要求没有新增未解释失败，基线的那个也不该留着",
     "tests/test_auth.py 改用 contextlib.closing", "auth 用例通过"),
    ("verify", "v1 页面链路保持不动，v2 编排放进新文件 knowledge_pipeline.py",
     "规格明确禁止把 run_routed 改成表面返回 pages 内部写 Claim 的函数",
     "git diff ba0d4bd 对 wiki_pipeline.py/core.py/store.py 为空", "R1 约束满足"),
    ("verify", "全量套件 298 个用例全绿，生成副本与 canonical 字节一致",
     "完成定义的前两条可证伪断言",
     "reports/test-baseline.md", "298 通过 0 失败 0 跳过，--check 无 stale"),
    ("open", "真实模型 holdout 三次运行未执行，真实材料 gold 未人工确认",
     "缺 API key，且机器不能给自己签收质量标签",
     "reports/knowledge-v2/release-report.md 第 5 节", "标注未执行，且 run_eval 缺条件时拒绝出报告"),
]


def main() -> None:
    header = "ts\tphase\tdecision\twhy\tevidence\tresult\n"
    if not TRAIL.exists() or not TRAIL.read_text(encoding="utf-8").startswith("ts\t"):
        TRAIL.write_text(header, encoding="utf-8")
    lines = [TRAIL.read_text(encoding="utf-8").rstrip("\n")]
    stamp = "2026-09-17T14:40:00Z"
    for phase, decision, why, evidence, result in ROWS:
        cells = [stamp, phase, decision, why, evidence, result]
        cleaned = [
            value.replace("\t", " ").replace("\n", " ").strip() or "-"
            for value in cells
        ]
        lines.append("\t".join(cleaned))
    TRAIL.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(ROWS)} rows to {TRAIL}")


if __name__ == "__main__":
    main()
