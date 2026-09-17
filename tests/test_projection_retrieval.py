"""Q01 to Q10: pages, search, Why chains and quotes, proven on a real store.

Every test here runs against a real `ClaimStore` in a temporary directory, with
real frozen revisions, real registered evidence and real commits. Nothing is
mocked, because the cases are about what the knowledge layer actually holds.
"""

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).parent))

from acceptance import case  # noqa: E402

from llm_wiki_mcp import projection, retrieval  # noqa: E402
from llm_wiki_mcp.claim_store import ClaimStore
from llm_wiki_mcp.knowledge_service import KnowledgeService  # noqa: E402
from llm_wiki_mcp.chunking import artifact_structure  # noqa: E402
from llm_wiki_mcp.evidence import freeze_artifact, make_evidence, normalize_text  # noqa: E402
from llm_wiki_mcp.knowledge_types import KnowledgeError, Scope, build_change_set  # noqa: E402

CAUSAL_CONNECTIVES = (
    "因为",
    "所以",
    "因此",
    "导致",
    "由此",
    "故而",
    "because",
    "therefore",
    "thus",
    "hence",
)
"""Words that would turn two independent claims into one conclusion the claims
never made. The renderer has no vocabulary for them, and this list is how a test
says so out loud."""


class KnowledgeCase(unittest.TestCase):
    """A store, a scope, and the fixtures each case commits for itself."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ClaimStore(Path(self.temporary.name) / "knowledge.sqlite3")
        self.scope = Scope.of("local", "project-a")
        self.sequence = 0

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    def add_source(self, text, *, source_id="src-note", source_type="note", label="note"):
        revision = self.store.freeze_revision(
            scope=self.scope,
            source_id=source_id,
            source_type=source_type,
            label=label,
            raw_content=text,
        )
        artifact = freeze_artifact(
            revision_id=revision["revision_id"],
            text=text,
            parser_name="structural",
            parser_version="1",
            config_hash="cfg-1",
            structure=artifact_structure(normalize_text(text)),
        )
        self.store.store_artifact(scope=self.scope, artifact=artifact)
        return artifact

    def add_evidence(self, artifact, needle):
        text = artifact.normalized_text
        start = text.index(needle)
        record = make_evidence(
            project_id=self.scope.project_id,
            artifact=artifact,
            spans=[(start, start + len(needle))],
        )
        self.store.register_evidence(record, scope=self.scope)
        return record.evidence_id

    def claim(
        self,
        statement,
        *,
        claim_id=None,
        kind="fact",
        derivation="explicit",
        epistemic="asserted",
        lifecycle="active",
        grounding="grounded",
        decision=None,
        question=None,
        evidence=(),
        premises=(),
        inference_note="",
        assumptions=(),
        conditions=(),
        subjects=(),
        attribution=None,
        support=(),
    ):
        payload = {
            "statement": statement,
            "state": {
                "knowledge_kind": kind,
                "derivation": derivation,
                "epistemic_status": epistemic,
                "lifecycle_status": lifecycle,
                "grounding_status": grounding,
                "decision_state": decision,
                "question_state": question,
            },
            "conditions": list(conditions),
            "subjects": list(subjects),
            "support": list(support),
            "origins": [],
        }
        if claim_id:
            payload["claim_id"] = claim_id
        if attribution:
            payload["attribution"] = dict(attribution)
        if evidence:
            payload["origins"].append({"derivation": "explicit", "evidence_refs": list(evidence)})
        if premises:
            payload["origins"].append(
                {
                    "derivation": "synthesized",
                    "inference_note": inference_note or "由已记录的前提推导。",
                    "premise_claim_version_ids": list(premises),
                    "assumptions": list(assumptions),
                }
            )
        return payload

    def commit(self, claims=(), *, relations=(), reviews=(), dirty_pages=None):
        self.sequence += 1
        base = self.store.current_version(self.scope.project_id)
        changeset = build_change_set(
            knowledge_space_id=self.scope.knowledge_space_id,
            project_id=self.scope.project_id,
            run_id=f"run-{self.sequence}",
            claims=claims,
            relations=relations,
            reviews=reviews,
            base_version=base,
        )
        if dirty_pages:
            changeset["dirty_pages"] = list(dirty_pages)
        return self.store.commit_changes(
            actor_subject="tester",
            base_version=base,
            idempotency_key=f"key-{self.sequence}",
            changeset=changeset,
            project_id=self.scope.project_id,
        )

    def row(self, claim_id):
        versions = self.version_rows(claim_id)
        current = self.store.get_claim(claim_id, self.scope)["current_version_id"]
        return next(row for row in versions if row["claim_version_id"] == current)

    def version_rows(self, claim_id):
        return [
            row
            for row in self.store.iter_claims(self.scope, include_history=True)
            if row["claim_id"] == claim_id
        ]

    def committed_versions(self):
        return {row["claim_version_id"] for row in self.store.iter_claims(self.scope, include_history=True)}

    # ------------------------------------------------------------------
    # The caller's own writes: projection rows and redirect rows
    # ------------------------------------------------------------------

    def page_id_for(self, slug):
        return "pag_" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:32]

    def write_page(self, page, *, slug=None, title=None, projection_status="current", dirty=0):
        manifest = page["manifest"]
        slug = slug or manifest["page_slug"]
        connection = sqlite3.connect(self.store.database)
        try:
            connection.execute(
                """INSERT OR REPLACE INTO page_projections(
                       page_id, knowledge_space_id, project_id, slug, title, page_kind, renderer_version,
                       manifest_json, markdown, content_sha256, projection_status, dirty, rendered_at)
                   VALUES (?, ?, ?, ?, ?, 'topic', ?, ?, ?, ?, ?, ?, '2026-09-17T00:00:00Z')""",
                (
                    self.page_id_for(slug),
                    self.scope.knowledge_space_id,
                    self.scope.project_id,
                    slug,
                    title or manifest["title"],
                    manifest["renderer_version"],
                    json.dumps(manifest, ensure_ascii=False),
                    page["markdown"],
                    page["content_sha256"],
                    projection_status,
                    dirty,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def write_redirect(self, record, *, page_id):
        connection = sqlite3.connect(self.store.database)
        try:
            connection.execute(
                """INSERT OR REPLACE INTO page_redirects(
                       knowledge_space_id, project_id, old_slug, page_id, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, '2026-09-17T00:00:00Z')""",
                (
                    self.scope.knowledge_space_id,
                    self.scope.project_id,
                    record["old_slug"],
                    page_id,
                    record["reason"],
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def raw_page_rows(self):
        """The same rows exactly as the table stores them, manifest still JSON."""

        connection = sqlite3.connect(self.store.database)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT slug, title, renderer_version, manifest_json, projection_status, dirty FROM page_projections WHERE project_id = ?",
                (self.scope.project_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def read_pages(self):
        connection = sqlite3.connect(self.store.database)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """SELECT slug, title, markdown, content_sha256, projection_status, dirty,
                          renderer_version, manifest_json
                   FROM page_projections WHERE project_id = ?""",
                (self.scope.project_id,),
            ).fetchall()
        finally:
            connection.close()
        return {
            row["slug"]: {**dict(row), "manifest": json.loads(row["manifest_json"])} for row in rows
        }

    def read_redirects(self):
        connection = sqlite3.connect(self.store.database)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT old_slug, page_id, reason FROM page_redirects WHERE project_id = ?",
                (self.scope.project_id,),
            ).fetchall()
        finally:
            connection.close()
        pages = {row["page_id"]: row["slug"] for row in self._page_id_rows()}
        return [
            {"old_slug": row["old_slug"], "new_slug": pages.get(row["page_id"], ""), "reason": row["reason"]}
            for row in rows
        ]

    def _page_id_rows(self):
        connection = sqlite3.connect(self.store.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute("SELECT page_id, slug FROM page_projections").fetchall()
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Shared assertions
    # ------------------------------------------------------------------

    def assert_page_is_derivable(self, page, claims, *, title):
        """Every line of the page comes from a claim, a tag, or the page metadata."""

        statements = {claim["statement"] for claim in claims}
        conditions = {str(item) for claim in claims for item in claim.get("conditions") or ()}
        notes = set()
        for claim in claims:
            for origin in claim.get("origins") or ():
                if str(origin.get("derivation")) == "synthesized":
                    notes.add(str(origin.get("inference_note") or ""))
        assumptions = {
            str(item)
            for claim in claims
            for origin in claim.get("origins") or ()
            for item in origin.get("assumptions") or ()
        }
        attributions = set()
        for claim in claims:
            attributions.add(str(claim.get("asserted_by") or ""))
            attributions.add(str(claim.get("asserted_at") or ""))
        labels = {claim["statement"]: "".join(projection.labels_for_claim(claim)) for claim in claims}
        known_lines = {f"# {title}"} | {f"## {section}" for section in projection.SECTIONS}

        for line in page["markdown"].splitlines():
            if not line.strip() or line in known_lines:
                continue
            if line.startswith("> 页面："):
                continue
            if line.startswith("**"):
                body = line[2:]
                statement, _, tags = body.partition("**")
                self.assertIn(statement, statements, f"a paragraph states something no claim says: {line!r}")
                self.assertEqual(labels[statement], tags, "a paragraph carries tags its claim did not earn")
                continue
            for prefix, values in (
                ("- 条件：", conditions),
                ("- 推导说明：", notes),
                ("- 假设：", assumptions),
            ):
                if line.startswith(prefix):
                    self.assertIn(line[len(prefix) :], values, f"line not in the claim: {line!r}")
                    break
            else:
                if line.startswith("- 归因："):
                    for part in line[len("- 归因：") :].split(" · "):
                        self.assertIn(part.replace("记录时间 ", ""), attributions | {""})
                    continue
                self.fail(f"the renderer wrote a line no claim supports: {line!r}")

    @staticmethod
    def paragraph_tags(line):
        """The tag text a rendered paragraph carries after its statement."""

        return line[2:].split("**", 1)[1]

    def paragraphs_by_section(self, markdown):
        """section -> the claim statements rendered under it."""

        sections = {}
        current = None
        for line in markdown.splitlines():
            if line.startswith("## "):
                current = line[3:]
                sections.setdefault(current, [])
                continue
            if line.startswith("**") and current:
                sections[current].append(line[2:].split("**", 1)[0])
        return sections


class ProjectionTests(KnowledgeCase):
    @case("Q01")
    def test_q01_every_paragraph_is_a_committed_claim_version(self):
        artifact = self.add_source("原文一：缓存层使用 Redis。\n原文二：主库使用 PostgreSQL。")
        first = self.add_evidence(artifact, "缓存层使用 Redis。")
        second = self.add_evidence(artifact, "主库使用 PostgreSQL。")
        outcome = self.commit(
            [
                self.claim("缓存层使用 Redis。", evidence=[first]),
                self.claim("主库使用 PostgreSQL。", evidence=[second]),
            ]
        )
        self.assertEqual(2, outcome.created_claims)
        claims = [self.row(claim_id) for claim_id in outcome.created_claim_ids]

        page = projection.render_page(
            page_slug="architecture",
            title="架构说明",
            claims=claims,
            topics=("架构",),
            generated_at="2026-09-17T00:00:00Z",
        )
        manifest = page["manifest"]
        self.assertEqual("projection/1", manifest["renderer_version"])
        self.assertEqual("architecture", manifest["page_slug"])
        self.assertEqual(2, len(manifest["entries"]))
        committed = self.committed_versions()
        for entry in manifest["entries"]:
            self.assertIn(entry["claim_version_id"], committed)
            self.assertIn(entry["claim_id"], outcome.created_claim_ids)
        self.assertEqual(
            sorted(entry["claim_version_id"] for entry in manifest["entries"]),
            manifest["claim_versions"],
        )

        paragraphs = [line for line in page["markdown"].splitlines() if line.startswith("**")]
        self.assertEqual(len(manifest["entries"]), len(paragraphs))
        self.assertEqual({claim["statement"] for claim in claims}, {line.split("**")[1] for line in paragraphs})

        for connective in CAUSAL_CONNECTIVES:
            self.assertNotIn(connective, page["markdown"], "the page joined claims no claim joined")
        self.assert_page_is_derivable(page, claims, title="架构说明")
        self.assertEqual("current", page["projection_status"])
        self.assertEqual(projection.page_content_sha256(page["markdown"]), page["content_sha256"])

        reversed_page = projection.render_page(
            page_slug="architecture",
            title="架构说明",
            claims=list(reversed(claims)),
            topics=("架构",),
            generated_at="2026-09-17T00:00:00Z",
        )
        self.assertEqual(page["markdown"], reversed_page["markdown"])

    @case("Q02")
    def test_q02_each_status_keeps_its_own_wording(self):
        artifact = self.add_source("原文：可能引入缓存层。\n原文：建议先做基准测试。\n原文：主库方案已采纳。\n原文：旧方案被新方案替代。")
        hypothesis_evidence = self.add_evidence(artifact, "可能引入缓存层。")
        proposed_evidence = self.add_evidence(artifact, "建议先做基准测试。")
        adopted_evidence = self.add_evidence(artifact, "主库方案已采纳。")
        replaced_evidence = self.add_evidence(artifact, "旧方案被新方案替代。")
        outcome = self.commit(
            [
                self.claim("可能引入缓存层。", epistemic="hypothesis", evidence=[hypothesis_evidence]),
                self.claim(
                    "建议先做基准测试。",
                    kind="decision",
                    decision="proposed",
                    evidence=[proposed_evidence],
                ),
                self.claim(
                    "主库方案已经采纳。",
                    kind="decision",
                    decision="adopted",
                    support=["adoption_record"],
                    evidence=[adopted_evidence],
                ),
                self.claim(
                    "旧方案被新方案替代。",
                    kind="judgment",
                    lifecycle="superseded",
                    evidence=[replaced_evidence],
                ),
            ]
        )
        self.assertEqual(4, outcome.created_claims)
        claims = [self.row(claim_id) for claim_id in outcome.created_claim_ids]
        page = projection.render_page(
            page_slug="statuses",
            title="状态标签",
            claims=claims,
            generated_at="2026-09-17T00:00:00Z",
        )
        markdown = page["markdown"]
        for tag in ("（假设）", "（提议，未采纳）", "（已采纳）", "（已替代）"):
            self.assertIn(tag, markdown)

        paragraphs = {line.split("**")[1]: line for line in markdown.splitlines() if line.startswith("**")}
        self.assertEqual("（假设）", self.paragraph_tags(paragraphs["可能引入缓存层。"]))
        self.assertEqual("（提议，未采纳）", self.paragraph_tags(paragraphs["建议先做基准测试。"]))
        self.assertEqual("（已采纳）", self.paragraph_tags(paragraphs["主库方案已经采纳。"]))
        self.assertEqual("（已替代）", self.paragraph_tags(paragraphs["旧方案被新方案替代。"]))

        self.assertNotIn("（已采纳）", paragraphs["建议先做基准测试。"])
        self.assertNotIn("（已采纳）", paragraphs["可能引入缓存层。"])
        self.assertNotIn("（提议，未采纳）", paragraphs["主库方案已经采纳。"])

        sections = self.paragraphs_by_section(markdown)
        self.assertIn("主库方案已经采纳。", sections["决定"])
        self.assertIn("建议先做基准测试。", sections["决定"])
        self.assertIn("可能引入缓存层。", sections["当前判断"])
        self.assertIn("旧方案被新方案替代。", sections["历史变化"])
        self.assertNotIn("旧方案被新方案替代。", sections["当前判断"])
        self.assert_page_is_derivable(page, claims, title="状态标签")

    @case("Q03")
    def test_q03_a_move_changes_the_mapping_and_nothing_else(self):
        artifact = self.add_source("原文：页面移动不改变知识身份。")
        evidence_id = self.add_evidence(artifact, "页面移动不改变知识身份。")
        outcome = self.commit([self.claim("页面移动不改变知识身份。", evidence=[evidence_id])])
        claim_id = outcome.created_claim_ids[0]
        before_versions = [(row["claim_id"], row["claim_version_id"]) for row in self.version_rows(claim_id)]
        before_origins = json.dumps(self.store.get_claim(claim_id, self.scope)["origins"], ensure_ascii=False)
        claims = [self.row(claim_id)]

        moves = (
            ("old-rename", "new-rename", "rename"),
            ("old-merge", "new-merge", "merge"),
            ("old-split", "new-split", "split"),
            ("old-area/page", "new-area/page", "reorganize"),
        )
        records = []
        for old_slug, new_slug, kind in moves:
            page = projection.render_page(
                page_slug=new_slug,
                title=new_slug,
                claims=claims,
                generated_at="2026-09-17T00:00:00Z",
            )
            self.write_page(page)
            record = projection.plan_page_move(old_slug=old_slug, new_slug=new_slug, kind=kind)
            self.assertEqual({"old_slug", "new_slug", "reason", "status"}, set(record))
            self.assertEqual("redirected", record["status"])
            self.assertTrue(record["reason"])
            self.write_redirect(record, page_id=self.page_id_for(new_slug))
            records.append((record, kind))

        self.assertEqual(
            before_versions,
            [(row["claim_id"], row["claim_version_id"]) for row in self.version_rows(claim_id)],
        )
        self.assertEqual(before_origins, json.dumps(self.store.get_claim(claim_id, self.scope)["origins"], ensure_ascii=False))
        self.assertIn(evidence_id, before_origins)
        for record, _ in records:
            self.assertNotIn("claim", json.dumps(record))

        pages = self.read_pages()
        redirects = self.read_redirects()
        for old_slug, new_slug, _ in moves:
            resolved = projection.resolve_page_slug(slug=old_slug, pages=pages, redirects=redirects)
            self.assertEqual("redirected", resolved["status"], f"{old_slug} resolved like a hole")
            self.assertEqual(new_slug, resolved["page_slug"])
            self.assertTrue(resolved["reason"])
        missing = projection.resolve_page_slug(slug="never-existed", pages=pages, redirects=redirects)
        self.assertEqual("unknown", missing["status"])
        self.assertIsNone(missing["page_slug"])
        with self.assertRaises(ValueError):
            projection.plan_page_move(old_slug="a", new_slug="b", kind="shuffle")


class RetrievalTests(KnowledgeCase):
    @case("Q04")
    def test_q04_page_and_claim_search_do_not_count_one_claim_twice(self):
        artifact = self.add_source(
            "本项目在生产环境使用 PostgreSQL 作为主库，缓存层使用 Redis。\n写入延迟低于 5 毫秒。"
        )
        evidence_id = self.add_evidence(artifact, "本项目在生产环境使用 PostgreSQL 作为主库，缓存层使用 Redis。")
        outcome = self.commit(
            [
                self.claim("PostgreSQL 是本项目选定的主库。", kind="definition", evidence=[evidence_id]),
                self.claim("数据库选型在 2026 年 5 月完成。", evidence=[evidence_id]),
            ]
        )
        page_claim_id, bare_claim_id = outcome.created_claim_ids

        before = retrieval.search_knowledge(store=self.store, scope=self.scope, query="数据库选型")
        baseline = [item for item in before["results"] if item.get("claim_id") == page_claim_id]
        self.assertEqual(1, len(baseline))
        self.assertEqual("claim", baseline[0]["object_type"])

        page = projection.render_page(
            page_slug="database-choice",
            title="数据库选型",
            claims=[self.row(page_claim_id)],
            generated_at="2026-09-17T00:00:00Z",
        )
        self.write_page(page)

        found = retrieval.search_knowledge(store=self.store, scope=self.scope, query="数据库选型")
        object_types = {item["object_type"] for item in found["results"]}
        self.assertIn("page", object_types)
        self.assertIn("claim", object_types)
        self.assertEqual(1, len([item for item in found["results"] if item.get("claim_id") == page_claim_id]))
        self.assertIn(bare_claim_id, [item.get("claim_id") for item in found["results"]])
        for item in found["results"]:
            self.assertEqual(self.scope.project_id, item["project_id"])
            self.assertEqual(found["snapshot_version"], item["snapshot_version"])
            self.assertTrue(item["claim_version_id"])
        page_items = [item for item in found["results"] if item["object_type"] == "page"]
        self.assertEqual(page_claim_id, page_items[0]["claim_id"])
        self.assertEqual(self.row(page_claim_id)["claim_version_id"], page_items[0]["claim_version_id"])
        self.assertEqual("stored_page", page_items[0]["content_source"])

        context = retrieval.build_answer_context(
            store=self.store, scope=self.scope, results=found["results"], allow_source_fallback=False
        )
        self.assertFalse(context["used_source_text"])
        self.assertEqual(len(found["results"]), len(context["items"]))
        for item in context["items"]:
            self.assertFalse(item["source_text_included"])
        rendered_context = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("写入延迟低于", rendered_context)
        self.assertIn("PostgreSQL 是本项目选定的主库。", rendered_context)
        self.assertEqual(page["content_sha256"], page_items[0]["content_sha256"])

    @case("Q05")
    def test_q05_the_why_chain_separates_recorded_reasons_from_rebuilt_ones(self):
        artifact = self.add_source(
            "原文：回滚脚本已经演练。\n原文：监控覆盖了写入路径。\n"
            "原文：索引调整提升了查询性能。\n原文：基准测试显示写入延迟更低。\n"
            "原文：会议记录：先做基准测试，再决定迁移。"
        )
        fourth = self.add_evidence(artifact, "回滚脚本已经演练。")
        third = self.add_evidence(artifact, "监控覆盖了写入路径。")
        second = self.add_evidence(artifact, "索引调整提升了查询性能。")
        first = self.add_evidence(artifact, "基准测试显示写入延迟更低。")
        reason_evidence = self.add_evidence(artifact, "会议记录：先做基准测试，再决定迁移。")

        premises = self.commit(
            [
                self.claim("回滚脚本已经演练。", evidence=[fourth]),
                self.claim("监控覆盖了写入路径。", evidence=[third]),
                self.claim("索引调整提升了查询性能。", evidence=[second]),
                self.claim("基准测试显示写入延迟更低。", evidence=[first]),
            ]
        )
        premise_rows = [self.row(claim_id) for claim_id in premises.created_claim_ids]
        root_outcome = self.commit(
            [
                self.claim(
                    "迁移采用 PostgreSQL 作为主库。",
                    kind="judgment",
                    derivation="synthesized",
                    premises=[premise_rows[3]["claim_version_id"]],
                    inference_note="由基准测试结论推导迁移方向。",
                    assumptions=["基准测试环境与生产环境一致"],
                ),
                self.claim(
                    "先做基准测试，再决定迁移。",
                    kind="rationale",
                    evidence=[reason_evidence],
                ),
            ]
        )
        root_row, reason_row = [self.row(claim_id) for claim_id in root_outcome.created_claim_ids]
        self.commit(
            relations=[
                {
                    "relation_type": "derived_from",
                    "from_claim_version_id": premise_rows[3]["claim_version_id"],
                    "to_claim_version_id": premise_rows[2]["claim_version_id"],
                    "relation_status": "accepted",
                },
                {
                    "relation_type": "derived_from",
                    "from_claim_version_id": premise_rows[2]["claim_version_id"],
                    "to_claim_version_id": premise_rows[1]["claim_version_id"],
                    "relation_status": "accepted",
                },
                {
                    "relation_type": "derived_from",
                    "from_claim_version_id": premise_rows[1]["claim_version_id"],
                    "to_claim_version_id": premise_rows[0]["claim_version_id"],
                    "relation_status": "accepted",
                },
                {
                    "relation_type": "supports",
                    "from_claim_version_id": reason_row["claim_version_id"],
                    "to_claim_version_id": root_row["claim_version_id"],
                    "relation_status": "accepted",
                },
            ]
        )

        chain = retrieval.explain_claim(store=self.store, scope=self.scope, claim_id=root_row["claim_id"], max_depth=3)
        self.assertTrue(
            {
                "claim_id",
                "claim_version_id",
                "chain",
                "truncated",
                "reconstructed",
                "recorded",
                "continue_from",
            }
            <= set(chain),
            sorted(chain),
        )
        self.assertTrue(chain["recorded"])
        self.assertTrue(chain["reconstructed"])
        self.assertEqual(
            set(),
            {entry["claim_version_id"] for entry in chain["recorded"]}
            & {entry["claim_version_id"] for entry in chain["reconstructed"]},
        )
        self.assertEqual(reason_row["claim_version_id"], chain["recorded"][0]["claim_version_id"])
        self.assertEqual("explicit", chain["recorded"][0]["derivation"])
        self.assertEqual(root_row["claim_version_id"], chain["reconstructed"][0]["claim_version_id"])
        self.assertEqual("由基准测试结论推导迁移方向。", chain["reconstructed"][0]["inference_note"])
        self.assertEqual(["基准测试环境与生产环境一致"], chain["reconstructed"][0]["assumptions"])
        self.assertNotIn(reason_row["statement"], [entry["statement"] for entry in chain["reconstructed"]])
        self.assertNotIn(root_row["statement"], [entry["statement"] for entry in chain["recorded"]])

        self.assertTrue(chain["truncated"])
        self.assertEqual(
            [premise_rows[0]["claim_version_id"]],
            [entry["claim_version_id"] for entry in chain["continue_from"]],
        )
        self.assertEqual([0, 1, 1, 2, 3], [entry["depth"] for entry in chain["chain"]])
        roles = {entry["claim_version_id"]: entry["role"] for entry in chain["chain"]}
        self.assertEqual("root", roles[root_row["claim_version_id"]])
        self.assertEqual("support", roles[reason_row["claim_version_id"]])
        self.assertEqual("premise", roles[premise_rows[3]["claim_version_id"]])
        kinds = {entry["claim_version_id"]: entry["reason_kind"] for entry in chain["chain"]}
        self.assertEqual("reconstructed", kinds[root_row["claim_version_id"]])
        self.assertEqual("recorded", kinds[reason_row["claim_version_id"]])
        with self.assertRaises(KnowledgeError):
            retrieval.explain_claim(store=self.store, scope=self.scope, claim_id=root_row["claim_id"], mode="maybe")

    @case("Q06")
    def test_q06_a_quote_comes_from_the_frozen_artifact(self):
        source_sentence = "原文：我们决定先验证 PostgreSQL 的写入性能，再决定是否迁移。"
        artifact = self.add_source(f"{source_sentence}\n缓存层使用 Redis。")
        evidence_id = self.add_evidence(artifact, source_sentence)
        outcome = self.commit(
            [self.claim("迁移决策取决于 PostgreSQL 写入性能的验证结果。", evidence=[evidence_id])]
        )
        claim = self.row(outcome.created_claim_ids[0])
        page = projection.render_page(
            page_slug="migration",
            title="迁移",
            claims=[claim],
            generated_at="2026-09-17T00:00:00Z",
        )
        self.assertNotIn(source_sentence, page["markdown"])

        quote = retrieval.quote_evidence(store=self.store, scope=self.scope, evidence_id=evidence_id)
        self.assertTrue(quote["found"])
        start = artifact.normalized_text.index(source_sentence)
        self.assertEqual(artifact.normalized_text[start : start + len(source_sentence)], quote["exact_text"])
        self.assertEqual(source_sentence, quote["exact_text"])
        self.assertEqual([source_sentence], list(quote["segments"]))
        self.assertNotEqual(claim["statement"], quote["exact_text"])
        self.assertNotIn(quote["exact_text"], page["markdown"])
        self.assertEqual("artifact", quote["quote_origin"])
        self.assertFalse(quote["derived"])
        self.assertEqual(artifact.artifact_id, quote["artifact_id"])
        self.assertEqual("unicode_code_point", quote["offset_unit"])

        unknown = retrieval.quote_evidence(store=self.store, scope=self.scope, evidence_id="evd_" + "0" * 32)
        self.assertFalse(unknown["found"])
        self.assertEqual("EVIDENCE_NOT_FOUND", unknown["error_code"])
        self.assertIsNone(unknown["exact_text"])
        self.assertEqual([], unknown["segments"])

    @case("Q07")
    def test_q07_current_and_historical_decisions_keep_their_recorded_times(self):
        artifact = self.add_source(
            "原文：主库使用 MySQL。\n原文：主库改用 PostgreSQL。\n原文：备份策略待定。"
        )
        old_evidence = self.add_evidence(artifact, "主库使用 MySQL。")
        new_evidence = self.add_evidence(artifact, "主库改用 PostgreSQL。")
        undated_evidence = self.add_evidence(artifact, "备份策略待定。")
        outcome = self.commit(
            [
                self.claim(
                    "主库使用 MySQL。",
                    kind="decision",
                    decision="adopted",
                    lifecycle="superseded",
                    support=["adoption_record"],
                    evidence=[old_evidence],
                    attribution={"asserted_by": "meeting", "asserted_at": "2026-01-01", "asserted_at_precision": "day"},
                ),
                self.claim(
                    "主库改用 PostgreSQL。",
                    kind="decision",
                    decision="adopted",
                    support=["adoption_record"],
                    evidence=[new_evidence],
                    attribution={"asserted_by": "meeting", "asserted_at": "2026-05-01", "asserted_at_precision": "day"},
                ),
                self.claim(
                    "备份策略待定。",
                    kind="decision",
                    decision="adopted",
                    support=["adoption_record"],
                    evidence=[undated_evidence],
                ),
            ]
        )
        old_id, new_id, undated_id = outcome.created_claim_ids
        self.assertEqual("superseded", self.row(old_id)["lifecycle_status"])
        self.commit(
            relations=[
                {
                    "relation_type": "supersedes",
                    "from_claim_version_id": self.row(new_id)["claim_version_id"],
                    "to_claim_version_id": self.row(old_id)["claim_version_id"],
                    "relation_status": "accepted",
                }
            ]
        )
        self.assertEqual(1, len(self.store.relations_of_type("supersedes", self.scope)))

        current = retrieval.current_versus_historical(store=self.store, scope=self.scope, claim_id=old_id)
        self.assertEqual(new_id, current["current"]["claim_id"])
        self.assertEqual("adopted", current["current"]["decision_state"])
        self.assertEqual("2026-05-01", current["current"]["asserted_at"])
        self.assertEqual(current["current"]["claim_version_id"], current["selected"]["claim_version_id"])
        self.assertEqual([old_id], [entry["claim_id"] for entry in current["historical"]])

        historical = retrieval.current_versus_historical(
            store=self.store, scope=self.scope, claim_id=old_id, as_of="2026-02-01"
        )
        self.assertEqual(old_id, historical["selected"]["claim_id"])
        self.assertEqual("2026-01-01", historical["selected"]["asserted_at"])
        self.assertNotEqual(new_id, historical["selected"]["claim_id"])
        self.assertEqual(new_id, historical["current"]["claim_id"])
        self.assertEqual([new_id], [entry["claim_id"] for entry in historical["historical"]])
        self.assertEqual("2026-05-01", historical["historical"][0]["asserted_at"])

        undated = retrieval.current_versus_historical(
            store=self.store, scope=self.scope, claim_id=undated_id, as_of="2026-06-01"
        )
        self.assertIsNone(undated["selected"])
        self.assertEqual([undated_id], [entry["claim_id"] for entry in undated["time_unknown"]])
        self.assertIsNone(undated["time_unknown"][0]["asserted_at"])
        self.assertTrue(undated["time_unknown"][0]["time_unknown"])
        self.assertEqual("no_version_has_a_recorded_time_at_or_before_as_of", undated["reason"])

        bucket = retrieval.get_as_of(self.version_rows(undated_id), "2026-06-01")
        self.assertEqual([], bucket["in_effect"])
        self.assertEqual(
            [self.row(undated_id)["claim_version_id"]],
            [entry["claim_version_id"] for entry in bucket["time_unknown"]],
        )

    @case("Q08")
    def test_q08_a_hand_edited_page_becomes_a_manual_note_source(self):
        artifact = self.add_source("原文：缓存层使用 Redis。")
        evidence_id = self.add_evidence(artifact, "缓存层使用 Redis。")
        outcome = self.commit([self.claim("缓存层使用 Redis。", evidence=[evidence_id])])
        claim_id = outcome.created_claim_ids[0]
        page = projection.render_page(
            page_slug="cache",
            title="缓存",
            claims=[self.row(claim_id)],
            generated_at="2026-09-17T00:00:00Z",
        )
        on_disk = Path(self.temporary.name) / "cache.md"
        on_disk.write_text(page["markdown"], encoding="utf-8")
        recorded = projection.page_content_sha256(page["markdown"])
        self.assertEqual(recorded, projection.page_content_sha256(on_disk.read_text(encoding="utf-8")))
        self.assertEqual(
            {"edited": False, "action": "none"},
            projection.detect_manual_edit(recorded_sha256=recorded, on_disk_text=on_disk.read_text(encoding="utf-8")),
        )

        added = "用户补充：缓存层下周迁移到 Redis 7。"
        on_disk.write_text(page["markdown"] + "\n" + added + "\n", encoding="utf-8")
        detection = projection.detect_manual_edit(
            recorded_sha256=recorded, on_disk_text=on_disk.read_text(encoding="utf-8")
        )
        self.assertTrue(detection["edited"])
        self.assertEqual("manual_note", detection["action"])

        rerendered = projection.render_page(
            page_slug="cache",
            title="缓存",
            claims=[self.row(claim_id)],
            generated_at="2026-09-18T00:00:00Z",
        )
        # A rebuild cannot write the manual sentence back into the page, because
        # the claim set it renders from does not contain it.
        self.assertNotIn(added, rerendered["markdown"])

        # The production path is what must not overwrite the edit, so the page is
        # stored as manual and the real rebuild is run against it.
        service = KnowledgeService(self.store, knowledge_space_id=self.scope.knowledge_space_id)
        self.store.upsert_projection(
            scope=self.scope,
            slug="cache",
            title="缓存",
            markdown=on_disk.read_text(encoding="utf-8"),
            manifest=page["manifest"],
            content_sha256=projection.page_content_sha256(on_disk.read_text(encoding="utf-8")),
            manual_edit_hash=recorded,
        )
        outcome = service.rebuild_projections(project_id=self.scope.project_id, dirty_only=False)
        self.assertEqual(outcome["skipped"], ["cache"], "a manual page is reported, not rewritten")
        self.assertNotIn("cache", outcome["rebuilt"])
        kept = self.store.projection("cache", self.scope)["markdown"]
        self.assertEqual("manual", self.store.projection("cache", self.scope)["projection_status"])
        self.assertIn(added, kept)
        self.assertIn("缓存层使用 Redis。", kept)

        manual_source = self.add_source(
            added, source_id="manual-note-1", source_type="manual_note", label="手工补充"
        )
        manual_evidence = self.add_evidence(manual_source, added)
        manual_outcome = self.commit([self.claim(added, evidence=[manual_evidence])])
        manual_claim_id = manual_outcome.created_claim_ids[0]
        self.assertEqual(
            [self.row(manual_claim_id)["claim_version_id"]],
            self.store.claim_versions_for_evidence(manual_evidence, self.scope),
        )
        original = self.store.get_claim(claim_id, self.scope)
        cited = {item for origin in original["origins"] for item in origin["evidence_refs"]}
        self.assertEqual({evidence_id}, cited)
        self.assertNotIn(manual_evidence, cited)
        self.assertNotIn(original["current_version_id"], self.store.claim_versions_for_evidence(manual_evidence, self.scope))

    @case("Q09")
    def test_q09_a_stale_projection_answers_from_current_claims(self):
        artifact = self.add_source("原文：数据库选型采用 MySQL 作为主库。\n原文：数据库选型最终采用 PostgreSQL 17 作为主库。")
        first_evidence = self.add_evidence(artifact, "数据库选型采用 MySQL 作为主库。")
        other = self.add_source("原文：缓存层使用 Redis。")
        other_evidence = self.add_evidence(other, "缓存层使用 Redis。")
        outcome = self.commit(
            [
                self.claim("数据库选型采用 MySQL 作为主库。", kind="definition", evidence=[first_evidence]),
                self.claim("缓存层使用 Redis。", evidence=[other_evidence]),
            ]
        )
        claim_id, other_claim_id = outcome.created_claim_ids
        first_row = self.row(claim_id)
        page = projection.render_page(
            page_slug="database-choice",
            title="数据库选型",
            claims=[first_row],
            generated_at="2026-09-17T00:00:00Z",
        )
        self.write_page(page)
        other_page = projection.render_page(
            page_slug="cache-layer",
            title="缓存层",
            claims=[self.row(other_claim_id)],
            generated_at="2026-09-17T00:00:00Z",
        )
        self.write_page(other_page)

        second_evidence = self.add_evidence(artifact, "数据库选型最终采用 PostgreSQL 17 作为主库。")
        self.commit(
            [
                self.claim(
                    "数据库选型最终采用 PostgreSQL 17 作为主库。",
                    claim_id=claim_id,
                    kind="definition",
                    evidence=[second_evidence],
                )
            ],
            dirty_pages=["database-choice"],
        )
        stored = self.read_pages()["database-choice"]
        self.assertEqual(1, stored["dirty"])
        self.assertEqual("stale", stored["projection_status"])
        knowledge_version = self.store.current_version(self.scope.project_id)
        second_row = self.row(claim_id)
        self.assertNotEqual(first_row["claim_version_id"], second_row["claim_version_id"])

        plan = projection.plan_rebuild(
            existing_pages=list(self.read_pages().values()),
            claims=self.store.iter_claims(self.scope),
        )
        self.assertEqual(["database-choice"], plan["dirty"])
        self.assertIn("database-choice", plan["rebuild"])
        self.assertEqual(["cache-layer"], plan["unchanged"])
        self.assertNotIn("cache-layer", plan["rebuild"])
        raw_plan = projection.plan_rebuild(
            existing_pages=self.raw_page_rows(),
            claims=self.store.iter_claims(self.scope),
        )
        self.assertEqual(plan, raw_plan)
        requested = projection.plan_rebuild(
            existing_pages=list(self.read_pages().values()),
            claims=self.store.iter_claims(self.scope),
            requested_slugs=["cache-layer", "brand-new"],
        )
        self.assertEqual(["brand-new", "cache-layer", "database-choice"], requested["rebuild"])
        self.assertEqual(["cache-layer", "database-choice"], requested["dirty"])
        self.assertNotIn("brand-new", requested["dirty"])

        found = retrieval.search_knowledge(store=self.store, scope=self.scope, query="数据库选型")
        self.assertTrue(found["degraded"])
        self.assertIn("stale_projection", found["degraded_reason"])
        self.assertEqual(knowledge_version, found["snapshot_version"])
        pages = [item for item in found["results"] if item["object_type"] == "page"]
        self.assertEqual(1, len(pages))
        served = pages[0]
        self.assertEqual("stale_projection_fallback", served["content_source"])
        self.assertEqual("stale", served["projection_status"])
        self.assertEqual(second_row["claim_version_id"], served["claim_version_id"])
        self.assertEqual("数据库选型最终采用 PostgreSQL 17 作为主库。", served["statement"])
        self.assertNotEqual(stored["content_sha256"], served["content_sha256"])

        # The served result does not expose its whole markdown, so the content is
        # pinned in two steps: the served hash equals the fallback render's hash,
        # and that render's text carries the current wording and not the
        # superseded one. Together those say what was served, without asserting on
        # a value the test computed from the same call the served path made.
        expected = projection.stale_projection_fallback(
            claims=[self.row(claim_id)], page_slug="database-choice", title="数据库选型"
        )
        self.assertEqual(expected["content_sha256"], served["content_sha256"])
        self.assertIn("PostgreSQL 17", expected["markdown"])
        self.assertNotIn("MySQL", expected["markdown"])
        self.assertNotEqual(
            projection.page_content_sha256(expected["markdown"]),
            stored["content_sha256"],
            "the served content must not be the stored stale page",
        )

        self.assertNotIn(
            first_row["claim_version_id"],
            {item.get("claim_version_id") for item in found["results"]},
        )
        self.assertEqual(knowledge_version, self.store.current_version(self.scope.project_id))
        self.assertEqual(second_row["claim_version_id"], self.store.get_claim(claim_id, self.scope)["current_version_id"])
        self.assertEqual("active", self.row(claim_id)["lifecycle_status"])


class ChineseRetrievalTests(KnowledgeCase):
    @case("Q10")
    def test_q10_chinese_recall_and_raw_derived_source_fallback(self):
        chinese = "数据库选型采用 PostgreSQL 作为本项目的主库。"
        english = "The database is PostgreSQL, chosen as the primary store for this project."
        artifact = self.add_source(f"{chinese}\n{english}\n写入延迟低于 5 毫秒。")
        evidence_id = self.add_evidence(artifact, chinese)
        outcome = self.commit([self.claim(chinese, kind="definition", evidence=[evidence_id])])
        claim_id = outcome.created_claim_ids[0]

        query = "数据库选型"
        self.assertNotIn(" ", query)
        tokens = retrieval.tokenize(query)
        self.assertIn("数据", tokens)
        self.assertIn("据库", tokens)
        self.assertIn("选型", tokens)
        found = retrieval.search_knowledge(store=self.store, scope=self.scope, query=query)
        self.assertIn(claim_id, [item.get("claim_id") for item in found["results"] if item["object_type"] == "claim"])
        self.assertEqual(
            found["snapshot_version"],
            self.store.current_version(self.scope.project_id),
        )

        paraphrase = "which database is the primary store"
        missed = retrieval.search_knowledge(store=self.store, scope=self.scope, query=paraphrase)
        self.assertEqual([], [item for item in missed["results"] if item.get("claim_id") == claim_id])
        fallback = retrieval.source_fallback(
            store=self.store, scope=self.scope, query=paraphrase, claim_results=missed["results"]
        )
        self.assertTrue(fallback["found"])
        self.assertTrue(fallback["raw_derived"])
        self.assertTrue(fallback["items"])
        for item in fallback["items"]:
            self.assertTrue(item["raw_derived"])
            self.assertEqual("source", item["object_type"])
            self.assertEqual("", item["evidence_id"])
            start, end = item["spans"][0]
            self.assertEqual(item["matched_text"], artifact.normalized_text[start:end])
        self.assertTrue(any(english in item["matched_text"] for item in fallback["items"]))
        self.assertEqual("raw_text_with_no_claim_covering_the_query", fallback["reason"])

        empty = retrieval.source_fallback(
            store=self.store, scope=self.scope, query="量子引力波干涉仪校准流程", claim_results=[]
        )
        self.assertFalse(empty["found"])
        self.assertEqual([], empty["items"])
        self.assertTrue(empty["raw_derived"])
        self.assertTrue(empty["reason"])
        self.assertEqual("no_artifact_text_matches_query", empty["reason"])
        context = retrieval.build_answer_context(
            store=self.store, scope=self.scope, results=empty["items"], allow_source_fallback=True
        )
        self.assertEqual([], context["items"])
        self.assertFalse(context["used_source_text"])


if __name__ == "__main__":
    unittest.main()
