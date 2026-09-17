"""Wire the deterministic renderer into KnowledgeService."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "plugins" / "llm-wiki" / "llm_wiki_mcp" / "knowledge_service.py"
BLOCK = Path(__file__).with_name("renderer_block.py.txt")

source = TARGET.read_text(encoding="utf-8")
if "def projection_renderer(" in source:
    raise SystemExit("already spliced")
block = BLOCK.read_text(encoding="utf-8")

anchor = "class KnowledgeService:"
assert anchor in source, "class anchor"
source = source.replace(anchor, block + anchor, 1)

old_imports = """import hashlib
import json
from dataclasses import dataclass"""
new_imports = """import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass"""
assert old_imports in source, "imports anchor"
source = source.replace(old_imports, new_imports, 1)

old = '''    def rebuild_projections(self, *, project_id: str, dirty_only: bool = True) -> dict[str, Any]:
        """Re-render affected pages, leaving a manual edit alone.

        A failed render is reported and leaves the knowledge untouched. The claim
        layer is authoritative, so a page that could not be written is a stale
        projection, not a lost decision.
        """

        if self.renderer is None:
            return {"rebuilt": [], "unchanged": [], "failed": [], "skipped": [], "renderer": "none"}
        scope = self.scope(project_id)
        claims = self.store.iter_claims(scope)
        plan = self.renderer(store=self.store, scope=scope, claims=claims, dirty_only=dirty_only)
        rebuilt: list[str] = []
        failed: list[dict[str, Any]] = []
        for entry in plan.get("pages", []):
            try:
                rendered = self.renderer(store=self.store, scope=scope, claims=entry["claims"], page_slug=entry["slug"], title=entry["title"], render_only=True)
                self.store.upsert_projection(
                    scope=scope,
                    slug=entry["slug"],
                    title=entry["title"],
                    markdown=rendered["markdown"],
                    manifest=rendered["manifest"],
                    content_sha256=rendered["content_sha256"],
                )
                rebuilt.append(entry["slug"])
            except Exception as error:  # a failed projection must not roll back knowledge
                failed.append({"slug": entry["slug"], "error": type(error).__name__})
        return {
            "rebuilt": rebuilt,
            "unchanged": list(plan.get("unchanged", [])),
            "failed": failed,
            "skipped": list(plan.get("manual", [])),
            "renderer": "deterministic",
        }'''
new = '''    def rebuild_projections(self, *, project_id: str, dirty_only: bool = True) -> dict[str, Any]:
        """Re-render affected pages, leaving a manual edit alone.

        A failed render is reported and leaves the knowledge untouched. The claim
        layer is authoritative, so a page that could not be written is a stale
        projection, not a lost decision.
        """

        scope = self.scope(project_id)
        renderer = self.renderer or projection_renderer
        plan = renderer(
            store=self.store,
            scope=scope,
            claims=self.store.iter_claims(scope, include_history=True),
            dirty_only=dirty_only,
        )
        rebuilt: list[str] = []
        failed: list[dict[str, Any]] = []
        for entry in plan.get("pages", []):
            try:
                self.store.upsert_projection(
                    scope=scope,
                    slug=entry["slug"],
                    title=entry["title"],
                    markdown=entry["markdown"],
                    manifest=entry["manifest"],
                    content_sha256=entry["content_sha256"],
                    renderer_version=plan["renderer"],
                )
                rebuilt.append(entry["slug"])
            except Exception as error:  # a failed projection must not roll back knowledge
                failed.append({"slug": entry["slug"], "error": type(error).__name__})
        return {
            "rebuilt": rebuilt,
            "unchanged": list(plan.get("unchanged", [])),
            "failed": failed,
            "skipped": list(plan.get("manual", [])),
            "renderer": plan.get("renderer", "deterministic"),
        }'''
assert old in source, "rebuild anchor"
source = source.replace(old, new, 1)

TARGET.write_text(source, encoding="utf-8")
print("spliced", len(source.splitlines()), "lines")
