"""Replace the CLI knowledge block with the version that takes agent-authored candidates."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "skills" / "lw" / "scripts" / "wiki.py"
BLOCK = Path(__file__).with_name("wiki_cli_v2.py.txt")

source = WIKI.read_text(encoding="utf-8")
block = BLOCK.read_text(encoding="utf-8")

start = source.index("def read_candidates(") if "def read_candidates(" in source else source.index("def knowledge_home()")
end = source.index("def do_knowledge_search(")

source = source[:start] + block + "\n\n" + source[end:]

# The ingest command gains the candidates file, and prepare is a new command.
old_ingest = '''    knowledge_ingest.add_argument("--dry-run", action="store_true", help="freeze and report without committing")'''
new_ingest = '''    knowledge_ingest.add_argument("--candidates", help="JSON candidates file produced by the driving agent")
    knowledge_ingest.add_argument("--dry-run", action="store_true", help="freeze and report without committing")
    knowledge_prepare = command("knowledge-prepare", "freeze material and print the chunks an agent reads before writing candidates")
    knowledge_prepare.add_argument("--text", action="append", help="material text; repeatable")
    knowledge_prepare.add_argument("--file", action="append", help="material file; repeatable")
    knowledge_prepare.add_argument("--from-tree", action="store_true", help="ingest every eligible project file")
    knowledge_prepare.add_argument("--purpose", default="Capture durable project knowledge.")'''
if old_ingest not in source:
    raise SystemExit("ingest anchor not found")
source = source.replace(old_ingest, new_ingest, 1)

old_dispatch = '''        elif args.command == "knowledge-ingest":
            do_knowledge_ingest(root, args)'''
new_dispatch = '''        elif args.command == "knowledge-prepare":
            do_knowledge_prepare(root, args)
        elif args.command == "knowledge-ingest":
            do_knowledge_ingest(root, args)'''
if old_dispatch not in source:
    raise SystemExit("dispatch anchor not found")
source = source.replace(old_dispatch, new_dispatch, 1)

if "import chunking  # noqa: E402" in source and "import sqlite3" not in source:
    pass

WIKI.write_text(source, encoding="utf-8")
print("spliced", len(source.splitlines()), "lines")
