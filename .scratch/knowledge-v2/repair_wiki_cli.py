"""Restore the CLI helpers the second splice dropped, with explicit boundaries."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "skills" / "lw" / "scripts" / "wiki.py"
FIRST = Path(__file__).with_name("wiki_cli_block.py.txt")

source = WIKI.read_text(encoding="utf-8")
original = FIRST.read_text(encoding="utf-8")

BOUNDARIES = {
    "def knowledge_home()": "def project_id_for(",
    "def project_id_for(": "def knowledge_binding(",
    "def knowledge_binding(": "def load_knowledge_modules(",
    "def load_knowledge_modules(": "def knowledge_service_for(",
    "def knowledge_sources(": "def do_knowledge_ingest(",
}

missing = [name for name in BOUNDARIES if name not in source]
if not missing:
    raise SystemExit("nothing missing")

chunks = []
for name in missing:
    start = original.index(name)
    end = original.index(BOUNDARIES[name], start + 1)
    chunks.append(original[start:end].rstrip() + "\n\n\n")

anchor = "def read_candidates("
if anchor not in source:
    raise SystemExit("anchor not found")
source = source.replace(anchor, "".join(chunks) + anchor, 1)
WIKI.write_text(source, encoding="utf-8")
print("restored", missing)
