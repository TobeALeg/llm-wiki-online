"""Splice the rewritten claim-writing methods into claim_store.py."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp" / "claim_store.py"
BODY = Path(__file__).with_name("new_write_claim.py.txt")

source = STORE.read_text(encoding="utf-8")
body = BODY.read_text(encoding="utf-8")

replacements = [
    (
        """CREATE TABLE IF NOT EXISTS claim_support_requirements (
    support_group_id TEXT NOT NULL,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id) ON DELETE CASCADE,
    requirement_kind TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (support_group_id, requirement_kind, requirement_id)
);""",
        """CREATE TABLE IF NOT EXISTS claim_support_requirements (
    support_group_id TEXT NOT NULL,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id) ON DELETE CASCADE,
    requirement_kind TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (claim_version_id, support_group_id, requirement_kind, requirement_id)
);""",
    ),
    (
        '''                "SELECT requirement_kind, requirement_id, available FROM claim_support_requirements WHERE support_group_id = ? ORDER BY requirement_kind, position",
                (row["support_group_id"],),''',
        '''                """SELECT requirement_kind, requirement_id, available FROM claim_support_requirements
                   WHERE support_group_id = ? AND claim_version_id = ? ORDER BY requirement_kind, position""",
                (row["support_group_id"], claim_version_id),''',
    ),
    (
        """        created: list[str] = []
        updated: list[str] = []
        unchanged = 0
        warnings: list[str] = []""",
        """        created: list[str] = []
        updated: list[str] = []
        unchanged_count = 0
        warnings: list[str] = []""",
    ),
    (
        "            claim_id, created_now, unchanged = self._write_claim(",
        "            claim_id, created_now, is_unchanged = self._write_claim(",
    ),
    (
        """            if unchanged:
                unchanged += 1
            elif created_now:""",
        """            if is_unchanged:
                unchanged_count += 1
            elif created_now:""",
    ),
    (
        """            unchanged_claims=unchanged,
            warnings=tuple(warnings),""",
        """            unchanged_claims=unchanged_count,
            warnings=tuple(warnings),""",
    ),
    (
        """    @staticmethod
    def _claim_unchanged(
        *,
        db: sqlite3.Connection,
        claim_id: str,
        claim_version_id: str,""",
        """    @staticmethod
    def _claim_unchanged(
        *,
        db: sqlite3.Connection,
        claim_version_id: str,""",
    ),
    (
        """                claim_id=claim_id,
                claim_version_id=existing_claim["current_version_id"],""",
        """                claim_version_id=current_version_id,""",
    ),
]

for old, new in replacements:
    if old not in source:
        raise SystemExit(f"anchor not found: {old.splitlines()[0]!r}")
    source = source.replace(old, new, 1)

start = source.index("    def _write_claim(\n")
end = source.index("    @staticmethod\n    def _claim_unchanged(")
source = source[:start] + body + source[end:]

old_commit = """                committed_version = current + 1
                stamp = now_iso()
                outcome = self._write_changes("""
new_commit = """                stamp = now_iso()
                probe = self._write_changes(
                    db=db,
                    scope=scope,
                    changeset=changeset,
                    actor_subject=actor_subject,
                    knowledge_version=current,
                    stamp=stamp,
                    run_id=run,
                )
                if not self._changed_anything(probe):
                    # Nothing moved, so the version must not move either. A reader
                    # watching the version counter must not see activity where there
                    # was none.
                    quiet = CommitOutcome(**{**probe.as_dict(), "status": "noop"})
                    db.execute(
                        "INSERT INTO knowledge_submissions(knowledge_space_id, project_id, idempotency_key, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            scope.knowledge_space_id,
                            scope.project_id,
                            idempotency_key,
                            request_hash,
                            _json(quiet.as_dict()),
                            stamp,
                        ),
                    )
                    db.commit()
                    return quiet
                committed_version = current + 1
                outcome = self._write_changes("""
if old_commit not in source:
    raise SystemExit("commit anchor not found")
source = source.replace(old_commit, new_commit, 1)

helper_anchor = "    def _write_changes(\n"
helper = '''    @staticmethod
    def _changed_anything(outcome: CommitOutcome) -> bool:
        """Whether a commit produced anything a later reader could observe."""

        return bool(
            outcome.created_claims
            or outcome.updated_claims
            or outcome.new_relations
            or outcome.review_pending
            or outcome.dropped_candidates
        )

'''
source = source.replace(helper_anchor, helper + helper_anchor, 1)

STORE.write_text(source, encoding="utf-8")
print("spliced", len(source.splitlines()), "lines")
