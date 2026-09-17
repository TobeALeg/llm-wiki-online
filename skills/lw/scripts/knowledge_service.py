"""The v2 use cases, written once so Local and Shared cannot drift.

Local mode and shared mode differ in two things only: which database file backs
the store, and who the authenticated actor is. Everything semantic lives here so
that the same material, the same configuration and the same model output produce
the same change set in both. A second copy of this flow is how the two modes
quietly stop agreeing.

The knowledge extractor is injected rather than imported. That keeps this module
free of the model-calling layer, lets a test drive the whole flow with a
controlled extractor, and keeps the local path from needing a server package.

Standard library only, and no sibling import by package name, so the same bytes
work as the canonical module and as the vendored copy inside the skill package.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # pragma: no cover - the flat layout is the vendored skill copy
    from .chunking import artifact_structure, chunk_text
    from .claim_store import ClaimStore
    from .evidence import freeze_artifact, make_evidence, normalize_text
    from .knowledge_types import Scope, build_change_set
except ImportError:  # pragma: no cover - the packaged layout
    from chunking import artifact_structure, chunk_text  # type: ignore[no-redef]
    from claim_store import ClaimStore  # type: ignore[no-redef]
    from evidence import freeze_artifact, make_evidence, normalize_text  # type: ignore[no-redef]
    from knowledge_types import Scope, build_change_set  # type: ignore[no-redef]

Extractor = Callable[[dict[str, Any]], list[dict[str, Any]]]
"""Turn one frozen artifact into claim dicts.

`context` carries `scope`, `project_id`, `source_id`, `revision_id`, `artifact`,
`chunks`, `evidence_ids`, `base_version`, `purpose` and `history_reader`. The
returned dicts are the shape `knowledge_types.build_change_set` accepts.
"""

LEGACY_WRITE_REJECTED = "LEGACY_WRITE_REJECTED"


class KnowledgeServiceError(RuntimeError):
    """A use-case level refusal the caller is expected to act on."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IngestReport:
    """What one ingest did, in the shape a dry run and a release report both read."""

    run_id: str
    status: str
    project_id: str
    knowledge_version: int
    committed: bool
    source_ids: tuple[str, ...]
    revision_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    batches: tuple[dict[str, Any], ...]
    changeset: Mapping[str, Any]
    commit: Mapping[str, Any] | None
    unchanged_sources: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "project_id": self.project_id,
            "knowledge_version": self.knowledge_version,
            "committed": self.committed,
            "source_ids": list(self.source_ids),
            "revision_ids": list(self.revision_ids),
            "artifact_ids": list(self.artifact_ids),
            "evidence_ids": list(self.evidence_ids),
            "batches": [dict(batch) for batch in self.batches],
            "changeset": dict(self.changeset),
            "commit": dict(self.commit) if self.commit else None,
            "unchanged_sources": list(self.unchanged_sources),
        }


def config_fingerprint(*, parser_name: str, parser_version: str, config_hash: str, prompt_versions: Mapping[str, str]) -> str:
    """The identity of the pipeline configuration a run was produced under."""

    payload = {
        "parser_name": parser_name,
        "parser_version": parser_version,
        "config_hash": config_hash,
        "prompt_versions": dict(sorted(prompt_versions.items())),
    }
    return "cfg_" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class KnowledgeService:
    """The v2 use cases over one store, with one scope per call."""

    def __init__(
        self,
        store: ClaimStore,
        *,
        knowledge_space_id: str,
        extractor: Extractor | None = None,
        renderer: Callable[..., dict[str, Any]] | None = None,
    ):
        self.store = store
        self.knowledge_space_id = knowledge_space_id
        self.extractor = extractor
        self.renderer = renderer

    def scope(self, project_id: str) -> Scope:
        return Scope.of(self.knowledge_space_id, project_id)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(
        self,
        *,
        actor_subject: str,
        project_id: str,
        source_inputs: Iterable[Mapping[str, Any]],
        base_version: int,
        idempotency_key: str,
        purpose: str,
        run_id: str,
        dry_run: bool = False,
        parser_name: str = "structural",
        parser_version: str = "1",
        config_hash: str = "default",
        prompt_versions: Mapping[str, str] | None = None,
    ) -> IngestReport:
        """Freeze, extract, validate, and commit one batch of material.

        A dry run stops before the commit and returns the same change set, so a
        reviewer reads exactly what would have been written. The frozen revisions,
        artifacts and evidence are recorded either way, because an evidence address
        is only meaningful if it survives from the dry run to the real one.
        """

        if not actor_subject:
            raise KnowledgeServiceError("A verified actor subject is required.", code="MISSING_ACTOR")
        scope = self.scope(project_id)
        configuration = config_fingerprint(
            parser_name=parser_name,
            parser_version=parser_version,
            config_hash=config_hash,
            prompt_versions=prompt_versions or {},
        )
        sources = [dict(item) for item in source_inputs]
        coverage = {"sources": [str(item.get("source_id", "")) for item in sources], "batches": []}

        revision_ids: list[str] = []
        artifact_ids: list[str] = []
        evidence_ids: list[str] = []
        batches: list[dict[str, Any]] = []
        unchanged: list[str] = []
        claim_units: list[dict[str, Any]] = []

        self.store.create_run(
            scope=scope, run_id=run_id, config_fingerprint=configuration, coverage=coverage
        )
        self.store.advance_run(run_id, "preparing")

        for item in sources:
            source_id = str(item["source_id"])
            revision = self.store.freeze_revision(
                scope=scope,
                source_id=source_id,
                source_type=str(item.get("kind", "material")),
                label=str(item.get("label", source_id)),
                raw_content=str(item.get("content", "")),
                source_time=item.get("source_time"),
                raw_available=bool(item.get("raw_available", True)),
                origin_uri=str(item.get("origin_uri", "")),
            )
            revision_ids.append(revision["revision_id"])
            text = normalize_text(str(item.get("content", "")))
            artifact = freeze_artifact(
                revision_id=revision["revision_id"],
                text=text,
                parser_name=parser_name,
                parser_version=parser_version,
                config_hash=config_hash,
                structure=artifact_structure(text),
            )
            artifact_ids.append(artifact.artifact_id)

            fingerprint = self.store.record_fingerprint(
                scope=scope,
                fingerprint=artifact.normalized_sha256,
                artifact_id=artifact.artifact_id,
                run_id=run_id,
            )
            if fingerprint["seen"] and fingerprint["artifact_id"] == artifact.artifact_id:
                # The exact content was already extracted here. Append provenance
                # instead of running the extractor again over the same text.
                unchanged.append(source_id)

            chunks = chunk_text(artifact.normalized_text)
            self.store.store_artifact(scope=scope, artifact=artifact, chunks=chunks)

            chunk_evidence: dict[str, str] = {}
            for chunk in chunks:
                record = make_evidence(
                    project_id=scope.project_id,
                    artifact=artifact,
                    spans=chunk.evidence(),
                    heading_path=chunk.heading_path,
                    label=chunk.chunk_id,
                )
                self.store.register_evidence(record, scope=scope)
                chunk_evidence[chunk.chunk_id] = record.evidence_id
                evidence_ids.append(record.evidence_id)

            batch_id = f"batch-{len(batches) + 1:03d}"
            batches.append(
                {
                    "batch_id": batch_id,
                    "source_id": source_id,
                    "revision_id": revision["revision_id"],
                    "artifact_id": artifact.artifact_id,
                    "chunk_ids": [chunk.chunk_id for chunk in chunks],
                    "reused_content": source_id in unchanged,
                }
            )
            coverage["batches"].append({"batch_id": batch_id, "source_ids": [source_id]})
            self.store.record_run_item(
                run_id=run_id,
                batch_id=batch_id,
                status="done",
                result={"chunks": len(chunks), "reused": source_id in unchanged},
            )

            if source_id in unchanged:
                continue
            if self.extractor is None:
                # Recording the material is not extracting it. A run with no
                # extractor reports that plainly rather than reading as complete.
                self.store.record_run_item(
                    run_id=run_id,
                    batch_id=batch_id,
                    status="skipped",
                    result={"reason": "no_extractor_configured"},
                )
                continue
            result = self.extractor(
                {
                    "scope": scope,
                    "project_id": scope.project_id,
                    "source_id": source_id,
                    "revision_id": revision["revision_id"],
                    "artifact": artifact,
                    "chunks": chunks,
                    "evidence_ids": chunk_evidence,
                    "base_version": base_version,
                    "purpose": purpose,
                    "history_reader": lambda candidate: self.store.iter_claims(scope),
                }
            )
            for unit in result:
                claim_units.append(dict(unit))

        if self.extractor is None and not unchanged:
            self.store.advance_run(run_id, "failed", error_code="NO_EXTRACTOR")
            report = IngestReport(
                run_id=run_id,
                status="failed",
                project_id=scope.project_id,
                knowledge_version=self.store.current_version(scope.project_id),
                committed=False,
                source_ids=tuple(str(item.get("source_id", "")) for item in sources),
                revision_ids=tuple(revision_ids),
                artifact_ids=tuple(artifact_ids),
                evidence_ids=tuple(evidence_ids),
                batches=tuple(batches),
                changeset={},
                commit=None,
                unchanged_sources=tuple(unchanged),
            )
            return report

        self.store.advance_run(run_id, "extracting")
        changeset = build_change_set(
            knowledge_space_id=scope.knowledge_space_id,
            project_id=scope.project_id,
            run_id=run_id,
            base_version=base_version,
            claims=claim_units,
        )
        self.store.advance_run(run_id, "validating")
        self.store.advance_run(run_id, "ready_to_commit")
        if dry_run:
            return IngestReport(
                run_id=run_id,
                status="ready_to_commit",
                project_id=scope.project_id,
                knowledge_version=self.store.current_version(scope.project_id),
                committed=False,
                source_ids=tuple(str(item.get("source_id", "")) for item in sources),
                revision_ids=tuple(revision_ids),
                artifact_ids=tuple(artifact_ids),
                evidence_ids=tuple(evidence_ids),
                batches=tuple(batches),
                changeset=changeset,
                commit=None,
                unchanged_sources=tuple(unchanged),
            )

        commit = self.store.commit_changes(
            actor_subject=actor_subject,
            base_version=base_version,
            idempotency_key=idempotency_key,
            changeset=changeset,
            run_id=run_id,
            project_id=scope.project_id,
        )
        self.store.advance_run(run_id, "committed")
        self.store.advance_run(run_id, "projecting")
        projection = self.rebuild_projections(project_id=project_id, dirty_only=True)
        # A page is pending a rebuild when a render failed, or when something was
        # marked dirty and no renderer is wired to clear it. Reporting completed
        # with a stale page on disk is what this avoids.
        pending = bool(projection["failed"]) or (
            bool(changeset.get("dirty_pages")) and self.renderer is None
        )
        final_status = "committed_projection_pending" if pending else "completed"
        self.store.advance_run(run_id, final_status)
        return IngestReport(
            run_id=run_id,
            status=final_status,
            project_id=scope.project_id,
            knowledge_version=commit.knowledge_version,
            committed=True,
            source_ids=tuple(str(item.get("source_id", "")) for item in sources),
            revision_ids=tuple(revision_ids),
            artifact_ids=tuple(artifact_ids),
            evidence_ids=tuple(evidence_ids),
            batches=tuple(batches),
            changeset=changeset,
            commit=commit.as_dict(),
            unchanged_sources=tuple(unchanged),
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def status(self, project_id: str) -> dict[str, Any]:
        scope = self.scope(project_id)
        claims = self.store.iter_claims(scope)
        by_kind: dict[str, int] = {}
        for claim in claims:
            by_kind[claim["knowledge_kind"]] = by_kind.get(claim["knowledge_kind"], 0) + 1
        return {
            "project_id": scope.project_id,
            "knowledge_space_id": scope.knowledge_space_id,
            "knowledge_version": self.store.current_version(project_id),
            "claim_count": len(claims),
            "claims_by_kind": by_kind,
            "review_pending": len(self.store.open_reviews(scope)),
        }

    def claim(self, project_id: str, claim_id: str, *, version: int | None = None) -> dict[str, Any]:
        return self.store.get_claim(claim_id, self.scope(project_id), version=version)

    def evidence(self, project_id: str, evidence_id: str) -> dict[str, Any]:
        return self.store.load_evidence(evidence_id, self.scope(project_id)).as_dict()

    def open_reviews(self, project_id: str) -> list[dict[str, Any]]:
        return self.store.open_reviews(self.scope(project_id))

    def review(
        self,
        *,
        actor_subject: str,
        project_id: str,
        review_id: str,
        expected_version: str,
        action: str,
        idempotency_key: str,
        note: str = "",
        edited_statement: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.review_action(
            actor_subject=actor_subject,
            review_id=review_id,
            expected_version=expected_version,
            action=action,
            scope=self.scope(project_id),
            idempotency_key=idempotency_key,
            note=note,
            edited_statement=edited_statement,
            topic_id=topic_id,
        )

    def search(self, project_id: str, query: str, *, limit: int = 10, kinds: Sequence[str] | None = None) -> dict[str, Any]:
        try:
            from .retrieval import search_knowledge  # type: ignore[import-not-found]
        except ImportError:
            try:
                from retrieval import search_knowledge  # type: ignore[import-not-found,no-redef]
            except ImportError as error:
                raise KnowledgeServiceError(
                    "The retrieval channel is not installed in this build.", code="NO_RETRIEVAL"
                ) from error
        return search_knowledge(store=self.store, scope=self.scope(project_id), query=query, limit=limit, kinds=kinds)

    def explain(self, project_id: str, claim_id: str, *, mode: str = "why", max_depth: int = 3) -> dict[str, Any]:
        try:
            from .retrieval import explain_claim  # type: ignore[import-not-found]
        except ImportError:
            try:
                from retrieval import explain_claim  # type: ignore[import-not-found,no-redef]
            except ImportError as error:
                raise KnowledgeServiceError(
                    "The retrieval channel is not installed in this build.", code="NO_RETRIEVAL"
                ) from error
        return explain_claim(
            store=self.store, scope=self.scope(project_id), claim_id=claim_id, mode=mode, max_depth=max_depth
        )

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def rebuild_projections(self, *, project_id: str, dirty_only: bool = True) -> dict[str, Any]:
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
        }


def semantic_changeset(report: IngestReport) -> dict[str, Any]:
    """The part of a report two modes must agree on, with storage noise removed.

    Actor names, generated ids, wall-clock times and storage paths differ between
    Local and Shared by construction. Comparing this projection is what lets a
    test assert the two modes reached the same knowledge without pretending the
    two databases are identical.
    """

    changeset = report.changeset
    return {
        "project_id": changeset.get("project_id"),
        "claims": sorted(
            (
                {
                    "statement": claim.get("statement"),
                    "state": claim.get("state"),
                    "conditions": sorted(claim.get("conditions", ())),
                    "subjects": sorted(claim.get("subjects", ())),
                    "origins": sorted(
                        (
                            {
                                "derivation": origin.get("derivation"),
                                "inference_note": origin.get("inference_note", ""),
                                "assumptions": sorted(origin.get("assumptions", ())),
                            }
                            for origin in claim.get("origins", ())
                        ),
                        key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
                    ),
                }
                for claim in changeset.get("claims", ())
            ),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        ),
        "relations": sorted(
            (
                {
                    "relation_type": relation.get("relation_type"),
                    "relation_status": relation.get("relation_status", "proposed"),
                }
                for relation in changeset.get("relations", ())
            ),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        ),
        "dropped": sorted(
            (
                {"statement": item.get("statement"), "reason_codes": sorted(item.get("reason_codes", ()))}
                for item in changeset.get("dropped", ())
            ),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        ),
    }
