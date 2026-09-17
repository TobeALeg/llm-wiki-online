"""Add the v2 knowledge commands to the local CLI and its vendored core."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "skills" / "lw" / "scripts" / "wiki.py"
BLOCK = Path(__file__).with_name("wiki_cli_block.py.txt")

source = WIKI.read_text(encoding="utf-8")
block = BLOCK.read_text(encoding="utf-8")

if "def do_knowledge_ingest" in source:
    raise SystemExit("already spliced")

constants = '''WIKI_DIR = ".llm-wiki"
STATE_VERSION = 2
KNOWLEDGE_DB_NAME = "knowledge.sqlite3"
BINDING_NAME = "binding.json"
DEFAULT_KNOWLEDGE_SPACE = "local"
PROJECT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")'''
old_constants = '''WIKI_DIR = ".llm-wiki"
STATE_VERSION = 2'''
if old_constants not in source:
    raise SystemExit("constants anchor not found")
source = source.replace(old_constants, constants, 1)

parser_anchor = "def parser() -> argparse.ArgumentParser:"
if parser_anchor not in source:
    raise SystemExit("parser anchor not found")
source = source.replace(parser_anchor, block + parser_anchor, 1)

commands = '''    retrieve = command("context", "retrieve relevant wiki pages")
    retrieve.add_argument("query")
    retrieve.add_argument("--limit", type=int, default=5)

    command("knowledge-init", "bind this project to the local knowledge space")
    knowledge_ingest = command("knowledge-ingest", "freeze material and extract claims into the local knowledge store")
    knowledge_ingest.add_argument("--text", action="append", help="material text; repeatable")
    knowledge_ingest.add_argument("--file", action="append", help="material file; repeatable")
    knowledge_ingest.add_argument("--from-tree", action="store_true", help="ingest every eligible project file")
    knowledge_ingest.add_argument("--purpose", default="Capture durable project knowledge.")
    knowledge_ingest.add_argument("--actor", help="authenticated actor subject")
    knowledge_ingest.add_argument("--key", help="idempotency key")
    knowledge_ingest.add_argument("--run", help="run id")
    knowledge_ingest.add_argument("--dry-run", action="store_true", help="freeze and report without committing")
    command("knowledge-status", "show knowledge version, claim counts and open reviews")
    knowledge_search = command("knowledge-search", "search pages and claims in this project")
    knowledge_search.add_argument("query")
    knowledge_search.add_argument("--limit", type=int, default=10)
    knowledge_evidence = command("knowledge-evidence", "recover the exact source text behind a citation")
    knowledge_evidence.add_argument("evidence_id")
    knowledge_claim = command("knowledge-claim", "show one claim with its origins and history")
    knowledge_claim.add_argument("claim_id")
    knowledge_claim.add_argument("--version", type=int)
    knowledge_explain = command("knowledge-explain", "show why a claim is held")
    knowledge_explain.add_argument("claim_id")
    knowledge_explain.add_argument("--mode", default="why")
    knowledge_explain.add_argument("--depth", type=int, default=3)
    command("knowledge-reviews", "list open reviews")
    knowledge_review = command("knowledge-review", "record a review decision")
    knowledge_review.add_argument("review_id")
    knowledge_review.add_argument("--action", required=True)
    knowledge_review.add_argument("--expected-version", required=True)
    knowledge_review.add_argument("--note", default="")
    knowledge_review.add_argument("--statement", help="new wording, for action=edit")
    knowledge_review.add_argument("--topic", help="topic id, for action=confirm_identity")
    knowledge_review.add_argument("--actor")
    knowledge_review.add_argument("--key")
    return result'''
old_tail = '''    retrieve = command("context", "retrieve relevant wiki pages")
    retrieve.add_argument("query")
    retrieve.add_argument("--limit", type=int, default=5)
    return result'''
if old_tail not in source:
    raise SystemExit("parser tail anchor not found")
source = source.replace(old_tail, commands, 1)

dispatch = '''        elif args.command == "context":
            require_wiki(root)
            context(root, args.query, max(1, args.limit))
        elif args.command == "knowledge-init":
            binding = knowledge_binding(root)
            print(json.dumps({**binding, "knowledge_database": str(knowledge_home() / KNOWLEDGE_DB_NAME)}, ensure_ascii=False, indent=2))
        elif args.command == "knowledge-ingest":
            do_knowledge_ingest(root, args)
        elif args.command == "knowledge-status":
            do_knowledge_status(root, args)
        elif args.command == "knowledge-search":
            do_knowledge_search(root, args)
        elif args.command == "knowledge-evidence":
            do_knowledge_evidence(root, args)
        elif args.command == "knowledge-claim":
            do_knowledge_claim(root, args)
        elif args.command == "knowledge-explain":
            do_knowledge_explain(root, args)
        elif args.command == "knowledge-reviews":
            do_knowledge_reviews(root, args)
        elif args.command == "knowledge-review":
            do_knowledge_review(root, args)'''
old_dispatch = '''        elif args.command == "context":
            require_wiki(root)
            context(root, args.query, max(1, args.limit))'''
if old_dispatch not in source:
    raise SystemExit("dispatch anchor not found")
source = source.replace(old_dispatch, dispatch, 1)

error_clause = "    except (WikiError, OSError) as exc:"
if error_clause not in source:
    raise SystemExit("error anchor not found")
source = source.replace(error_clause, error_clause, 1)

WIKI.write_text(source, encoding="utf-8")
print("spliced", len(source.splitlines()), "lines")
