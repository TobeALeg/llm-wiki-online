"""Build the review page a person annotates the draft labels on.

The page is one self-contained HTML file with the units embedded, so it opens from
the filesystem with no server, no build step and no network. Annotations live in
localStorage while the page is open and come out through an export button.

The export is the interface back to the pipeline: `apply_review.py` reads it and
writes `gold.jsonl` with the units a person passed marked `human_confirmed`. Until
that export exists, nothing here counts as a label, which is why the page is a
review surface and not a report.

Usage::

    python evals/knowledge_v2/build_review_page.py
    python evals/knowledge_v2/build_review_page.py --draft evals/knowledge_v2/gold.draft.jsonl
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_DRAFT = HERE / "gold.draft.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "reports" / "knowledge-v2" / "review" / "index.html"

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>标注复核</title>
<style>
  :root {
    --ink: #1c1c1e; --muted: #6b6b70; --line: #e2e2e6; --paper: #fbfbfd;
    --pass: #1f7a4d; --fail: #b3261e; --unsure: #8a6d1f; --focus: #2b6cb0;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink);
    font: 15px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
  header { position: sticky; top: 0; z-index: 10; background: #fff;
    border-bottom: 1px solid var(--line); padding: 14px 20px; }
  h1 { margin: 0 0 6px; font-size: 17px; }
  .bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
  .bar strong { color: var(--ink); }
  progress { width: 200px; height: 8px; }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 6px;
    border: 1px solid var(--line); background: #fff; cursor: pointer; }
  button:hover { border-color: var(--focus); }
  button.primary { background: var(--ink); color: #fff; border-color: var(--ink); }
  button.on { border-color: var(--focus); background: #eef4fb; }
  main { padding: 18px 20px 80px; max-width: 1180px; }
  article { background: #fff; border: 1px solid var(--line); border-radius: 10px;
    padding: 16px 18px; margin-bottom: 14px; }
  article[data-verdict="pass"] { border-left: 4px solid var(--pass); }
  article[data-verdict="fail"] { border-left: 4px solid var(--fail); }
  article[data-verdict="unsure"] { border-left: 4px solid var(--unsure); }
  article.zero { box-shadow: inset 0 0 0 2px #f3d6d4; }
  .tags { display: flex; gap: 8px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-bottom: 10px; }
  .tag { background: #f2f2f5; border-radius: 999px; padding: 2px 9px; }
  .tag.origin-published_claim { background: #e7f4ec; color: #1f7a4d; }
  .tag.origin-final_gate_rejection { background: #fdeceb; color: #b3261e; }
  .tag.origin-review { background: #fbf3e0; color: #8a6d1f; }
  .tag.origin-value_gate_drop { background: #eeeef3; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .col h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted); font-weight: 600; }
  blockquote { margin: 0; padding: 10px 12px; background: #f7f7fa; border-radius: 8px;
    border-left: 3px solid var(--line); white-space: pre-wrap; word-break: break-word; }
  dl { margin: 0; font-size: 13px; }
  dt { color: var(--muted); margin-top: 8px; }
  dd { margin: 2px 0 0; word-break: break-word; }
  .notes { margin-top: 10px; font-size: 12px; color: var(--muted); }
  .notes li { margin-bottom: 3px; }
  .actions { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .actions button.pass.on { background: #e7f4ec; border-color: var(--pass); color: var(--pass); font-weight: 600; }
  .actions button.fail.on { background: #fdeceb; border-color: var(--fail); color: var(--fail); font-weight: 600; }
  .actions button.unsure.on { background: #fbf3e0; border-color: var(--unsure); color: var(--unsure); font-weight: 600; }
  label.zero { font-size: 13px; display: flex; gap: 6px; align-items: center; color: var(--muted); }
  .fields { margin-top: 12px; display: grid; gap: 8px; }
  .fields label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 3px; }
  input[type=text], textarea { font: inherit; font-size: 13px; width: 100%; padding: 7px 9px;
    border: 1px solid var(--line); border-radius: 6px; background: #fff; }
  textarea { min-height: 54px; resize: vertical; }
  .hide { display: none; }
  footer { position: fixed; bottom: 0; left: 0; right: 0; background: #fff;
    border-top: 1px solid var(--line); padding: 10px 20px; font-size: 13px;
    display: flex; gap: 12px; align-items: center; }
  footer .spacer { flex: 1; }
  .warn { color: var(--fail); }
  details { margin-top: 10px; font-size: 13px; }
  summary { cursor: pointer; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>标注复核 <span id="scope" style="font-weight:400;color:#6b6b70"></span></h1>
  <div class="bar">
    <span>已批注 <strong id="done">0</strong> / <span id="total">0</span></span>
    <progress id="prog" max="1" value="0"></progress>
    <span>通过 <strong id="c-pass">0</strong></span>
    <span>不通过 <strong id="c-fail">0</strong></span>
    <span>拿不准 <strong id="c-unsure">0</strong></span>
    <span>零容忍 <strong id="c-zero">0</strong></span>
  </div>
  <div id="persist" class="hide" style="margin-top:8px;font-size:13px;color:#b3261e">
    这个浏览器不允许本页保存进度（直接双击打开文件时常见）。批注只留在内存里，
    请适时点「导出批注」，刷新会丢。用
    <code>python -m http.server 8777 --directory reports/knowledge-v2/review</code>
    打开 http://127.0.0.1:8777/index.html 就能保存进度。
  </div>
  <div class="controls">
    显示：
    <button data-filter="all" class="on">全部</button>
    <button data-filter="todo">未批注</button>
    <button data-filter="pass">通过</button>
    <button data-filter="fail">不通过</button>
    <button data-filter="unsure">拿不准</button>
    <button data-filter="zero">零容忍</button>
    <span style="margin-left:8px">组：</span>
    <select id="group"><option value="">全部</option></select>
    <span style="margin-left:8px">来源：</span>
    <select id="origin">
      <option value="">全部</option>
      <option value="published_claim">拟保留</option>
      <option value="final_gate_rejection">被拒绝</option>
      <option value="value_gate_drop">被丢弃</option>
      <option value="review">待审</option>
    </select>
    <button id="clear-filter">清除筛选</button>
  </div>
</header>
<main id="list"></main>
<footer>
  <button id="export" class="primary">导出批注</button>
  <button id="copy">复制到剪贴板</button>
  <span class="spacer"></span>
  <span id="hint">快捷键：↑/↓ 切换，1 通过，2 不通过，3 拿不准，0 零容忍</span>
</footer>
<script id="units" type="application/json">__UNITS__</script>
<script id="meta" type="application/json">__META__</script>
<script>
const units = JSON.parse(document.getElementById('units').textContent);
const meta = JSON.parse(document.getElementById('meta').textContent);
const KEY = 'llmwiki-review-v1';

// A page opened straight from the filesystem may have no localStorage, and losing a
// review to a browser policy would be worse than losing the convenience. So the store
// degrades to memory and says so, and the export button is the way the work leaves.
let persist = true;
const store = {
  read() {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}'); }
    catch (error) { persist = false; return {}; }
  },
  write(value) {
    try { localStorage.setItem(KEY, JSON.stringify(value)); }
    catch (error) { persist = false; showPersistWarning(); }
  },
};
function showPersistWarning() {
  const node = document.getElementById('persist');
  if (node) { node.classList.remove('hide'); }
}
let state = store.read();
let filter = 'all', groupFilter = '', originFilter = '', cursor = 0;

function save() { store.write(state); }
function entry(id) { return state[id] || (state[id] = { verdict: '', comment: '', corrected: '', zero: false }); }

function text(value) {
  const node = document.createElement('div');
  node.textContent = value == null ? '' : String(value);
  return node.innerHTML;
}

function render() {
  const list = document.getElementById('list');
  const shown = units.filter(u => {
    const e = state[u.unit_id] || {};
    if (groupFilter && u.group_id !== groupFilter) return false;
    if (originFilter && u.draft_from !== originFilter) return false;
    if (filter === 'todo') return !e.verdict;
    if (filter === 'zero') return !!e.zero;
    if (filter !== 'all') return e.verdict === filter;
    return true;
  });
  list.innerHTML = shown.map((u, index) => {
    const e = state[u.unit_id] || {};
    const rows = [];
    rows.push(`<h3>左：材料与拟保留的内容</h3><blockquote>${text(u.expected_meaning)}</blockquote>`);
    return `<article data-id="${u.unit_id}" data-verdict="${e.verdict || ''}" class="${e.zero ? 'zero' : ''}">
      <div class="tags">
        <span class="tag">${text(u.unit_id)}</span>
        <span class="tag">${text(u.group_id)}</span>
        <span class="tag">${text(u.split)}</span>
        <span class="tag origin-${u.draft_from}">${text(meta.origin_label[u.draft_from] || u.draft_from)}</span>
        ${u.kind ? `<span class="tag">${text(u.kind)}</span>` : ''}
        ${u.must_keep === null ? '<span class="tag">需人决定</span>' : ''}
      </div>
      <div class="grid">
        <div class="col">${rows.join('')}</div>
        <div class="col">
          <h3>右：草稿单元</h3>
          <dl>
            <dt>must_keep</dt><dd>${text(u.must_keep)}</dd>
            ${u.required_qualifiers.length ? `<dt>required_qualifiers</dt><dd>${text(u.required_qualifiers.join('、'))}</dd>` : ''}
            ${u.forbidden_assertions.length ? `<dt>forbidden_assertions</dt><dd>${text(u.forbidden_assertions.join('；'))}</dd>` : ''}
            ${u.future_questions.length ? `<dt>future_questions</dt><dd>${text(u.future_questions.join(' / '))}</dd>` : ''}
            ${u.evidence_targets.length ? `<dt>证据位置</dt><dd>${text(u.evidence_targets.map(t => t.source_fixture_id + ' 字符 ' + t.char_range[0] + '–' + t.char_range[1] + (t.heading_path && t.heading_path.length ? ' · ' + t.heading_path.join('/') : '')).join(' / '))}</dd>` : ''}
          </dl>
          ${u.machine_notes.length ? `<details><summary>机器说明（${u.machine_notes.length}）</summary><ul class="notes">${u.machine_notes.map(n => `<li>${text(n)}</li>`).join('')}</ul></details>` : ''}
        </div>
      </div>
      <div class="actions">
        <button class="pass ${e.verdict === 'pass' ? 'on' : ''}" data-v="pass">通过</button>
        <button class="fail ${e.verdict === 'fail' ? 'on' : ''}" data-v="fail">不通过</button>
        <button class="unsure ${e.verdict === 'unsure' ? 'on' : ''}" data-v="unsure">拿不准</button>
        <label class="zero"><input type="checkbox" class="zero-box" ${e.zero ? 'checked' : ''}> 这是零容忍错误</label>
      </div>
      <div class="fields">
        <div>
          <label>改正后的表述（留空表示草稿的表述可用）</label>
          <input type="text" class="corrected" value="${text(e.corrected)}" placeholder="如果这条该留下但表述不对，在这里写对的说法">
        </div>
        <div>
          <label>批注</label>
          <textarea class="comment" placeholder="为什么通过、为什么不该保留、缺了什么限定">${text(e.comment)}</textarea>
        </div>
      </div>
    </article>`;
  }).join('') || '<p style="color:#6b6b70">这个筛选下没有条目。</p>';
  if (cursor >= shown.length) cursor = Math.max(0, shown.length - 1);
  updateCounts();
}

function updateCounts() {
  const values = Object.values(state);
  const done = values.filter(e => e.verdict).length;
  document.getElementById('done').textContent = done;
  document.getElementById('total').textContent = units.length;
  document.getElementById('c-pass').textContent = values.filter(e => e.verdict === 'pass').length;
  document.getElementById('c-fail').textContent = values.filter(e => e.verdict === 'fail').length;
  document.getElementById('c-unsure').textContent = values.filter(e => e.verdict === 'unsure').length;
  document.getElementById('c-zero').textContent = values.filter(e => e.zero).length;
  const prog = document.getElementById('prog');
  prog.value = units.length ? done / units.length : 0;
}

function setVerdict(id, verdict) {
  const e = entry(id);
  e.verdict = e.verdict === verdict ? '' : verdict;
  save();
  const card = document.querySelector(`article[data-id="${id}"]`);
  if (card) {
    card.dataset.verdict = e.verdict;
    card.querySelectorAll('.actions button[data-v]').forEach(b => b.classList.toggle('on', b.dataset.v === e.verdict));
  }
  updateCounts();
}

document.getElementById('list').addEventListener('click', event => {
  const button = event.target.closest('button[data-v]');
  if (button) { setVerdict(button.closest('article').dataset.id, button.dataset.v); return; }
});
document.getElementById('list').addEventListener('input', event => {
  const card = event.target.closest('article');
  if (!card) return;
  const e = entry(card.dataset.id);
  if (event.target.classList.contains('comment')) e.comment = event.target.value;
  if (event.target.classList.contains('corrected')) e.corrected = event.target.value;
  if (event.target.classList.contains('zero-box')) {
    e.zero = event.target.checked;
    card.classList.toggle('zero', e.zero);
    updateCounts();
  }
  save();
});

function applyFilter(name) {
  filter = name;
  document.querySelectorAll('.controls button[data-filter]').forEach(b => b.classList.toggle('on', b.dataset.filter === name));
  render();
}
document.querySelectorAll('.controls button[data-filter]').forEach(b => b.addEventListener('click', () => applyFilter(b.dataset.filter)));
document.getElementById('clear-filter').addEventListener('click', () => {
  groupFilter = ''; originFilter = '';
  document.getElementById('group').value = '';
  document.getElementById('origin').value = '';
  applyFilter('all');
});
document.getElementById('group').addEventListener('change', e => { groupFilter = e.target.value; render(); });
document.getElementById('origin').addEventListener('change', e => { originFilter = e.target.value; render(); });

function exportPayload() {
  return {
    exported_at: new Date().toISOString(),
    draft_file: meta.draft_file,
    unit_count: units.length,
    annotated: Object.values(state).filter(e => e.verdict).length,
    annotations: units.map(u => {
      const e = state[u.unit_id] || {};
      return {
        unit_id: u.unit_id,
        group_id: u.group_id,
        draft_from: u.draft_from,
        verdict: e.verdict || '',
        zero_tolerance: !!e.zero,
        comment: e.comment || '',
        corrected_meaning: e.corrected || '',
      };
    }),
  };
}
document.getElementById('export').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(exportPayload(), null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'review-annotations.json';
  link.click();
  URL.revokeObjectURL(link.href);
});
document.getElementById('copy').addEventListener('click', async () => {
  await navigator.clipboard.writeText(JSON.stringify(exportPayload()));
  document.getElementById('hint').textContent = '已复制到剪贴板，粘贴给我即可。';
});

document.addEventListener('keydown', event => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
  const cards = [...document.querySelectorAll('article')];
  if (!cards.length) return;
  if (event.key === 'ArrowDown' || event.key === 'j') { cursor = Math.min(cards.length - 1, cursor + 1); cards[cursor].scrollIntoView({ block: 'center' }); event.preventDefault(); }
  if (event.key === 'ArrowUp' || event.key === 'k') { cursor = Math.max(0, cursor - 1); cards[cursor].scrollIntoView({ block: 'center' }); event.preventDefault(); }
  const map = { '1': 'pass', '2': 'fail', '3': 'unsure' };
  if (map[event.key] && cards[cursor]) { setVerdict(cards[cursor].dataset.id, map[event.key]); }
});

(function init() {
  document.getElementById('scope').textContent = meta.scope;
  if (!persist) showPersistWarning();
  const groups = [...new Set(units.map(u => u.group_id))].sort();
  const select = document.getElementById('group');
  groups.forEach(g => {
    const option = document.createElement('option');
    option.value = g; option.textContent = g;
    select.appendChild(option);
  });
  document.getElementById('total').textContent = units.length;
  render();
})();
</script>
</body>
</html>
"""


def load_units(path: Path) -> list[dict]:
    units: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            units.append(json.loads(stripped))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} line {number} is not valid JSON: {error}") from error
    return units


def normalise(unit: dict) -> dict:
    """Only the fields the page shows, with the lists their template expects."""

    return {
        "unit_id": str(unit.get("unit_id") or ""),
        "group_id": str(unit.get("group_id") or ""),
        "split": str(unit.get("split") or ""),
        "draft_from": str(unit.get("draft_from") or ""),
        "kind": str(unit.get("kind") or ""),
        "must_keep": unit.get("must_keep"),
        "expected_meaning": str(unit.get("expected_meaning") or ""),
        "required_qualifiers": list(unit.get("required_qualifiers") or ()),
        "forbidden_assertions": list(unit.get("forbidden_assertions") or ()),
        "future_questions": list(unit.get("future_questions") or ()),
        "evidence_targets": [
            {
                "source_fixture_id": str(target.get("source_fixture_id") or ""),
                "char_range": list(target.get("char_range") or (0, 0)),
                "heading_path": list(target.get("heading_path") or ()),
            }
            for target in unit.get("evidence_targets") or ()
        ],
        "machine_notes": [str(note) for note in unit.get("machine_notes") or ()],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--draft", default=str(DEFAULT_DRAFT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--scope", default="", help="a line describing what is under review")
    arguments = parser.parse_args()

    draft_path = Path(arguments.draft).expanduser()
    if not draft_path.is_file():
        print(f"No draft at {draft_path}. Run draft_gold.py --write first.")
        return 2
    units = [normalise(unit) for unit in load_units(draft_path)]
    if not units:
        print(f"{draft_path} holds no units.")
        return 2

    groups = sorted({unit["group_id"] for unit in units})
    scope = arguments.scope
    if not scope:
        scope = (
            f"{len(units)} 条草稿 · {len(groups)} 个材料组 · "
            f"{', '.join(groups[:3])}{' 等' if len(groups) > 3 else ''}"
        )
    meta = {
        "draft_file": draft_path.name,
        "scope": scope,
        "origin_label": {
            "published_claim": "拟保留",
            "final_gate_rejection": "被拒绝",
            "value_gate_drop": "被丢弃",
            "review": "待审",
        },
        "generated_from": str(draft_path),
    }

    page = (
        PAGE.replace("__UNITS__", json.dumps(units, ensure_ascii=False))
        .replace("__META__", json.dumps(meta, ensure_ascii=False))
    )
    output = Path(arguments.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    print(f"wrote {output}")
    print(f"units: {len(units)} | groups: {len(groups)} | size: {len(page) // 1024} KB")
    print("打开这个文件即可批注；导出后把 JSON 交回来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
