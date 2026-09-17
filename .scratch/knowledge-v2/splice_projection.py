from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp" / "claim_store.py"
BLOCK = Path(__file__).with_name("projection_store.py.txt")

source = STORE.read_text(encoding="utf-8")
block = BLOCK.read_text(encoding="utf-8")
anchor = "    # ------------------------------------------------------------------\n    # Review\n    # ------------------------------------------------------------------"
if anchor not in source:
    raise SystemExit("anchor not found")
if "def upsert_projection" in source:
    raise SystemExit("already spliced")
STORE.write_text(source.replace(anchor, block + anchor, 1), encoding="utf-8")
print("spliced")
