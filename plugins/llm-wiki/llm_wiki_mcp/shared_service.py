"""Company Wiki use cases built on the authenticated subject and atomic store."""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable

from .claim_store import ClaimStore
from .core import ROUTED, Model, WikiCore, snapshot_chars, submit_mode
from .knowledge_service import Extractor, KnowledgeService
from .model import configured_model
from .store import DEFAULT_PROJECT_ID, IdempotencyError, SharedWikiStore, StoreError
from .wiki_pipeline import run_routed

KNOWLEDGE_V2_ENV = "LLM_WIKI_KNOWLEDGE_V2"
"""Turns the v2 write path on for this process. Off by default."""


def knowledge_v2_enabled() -> bool:
    return os.environ.get(KNOWLEDGE_V2_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class LegacyWriteRejected(StoreError):
    """A pre-v2 write that would bypass the claim layer.

    Returning the old success shape would let a caller believe a page edit became
    knowledge. The message names the v2 path to use instead, so an old client can
    be migrated rather than left guessing.
    """


class SharedWikiService:
    def __init__(
        self,
        store: SharedWikiStore,
        model: Model | None = None,
        *,
        knowledge: ClaimStore | None = None,
        extractor: Extractor | None = None,
        knowledge_space_id: str = "shared",
        knowledge_v2: bool | None = None,
        projection_renderer: Callable[..., dict[str, Any]] | None = None,
    ):
        self.store = store
        self.core = WikiCore(model or configured_model())
        self.knowledge_space_id = knowledge_space_id
        self.knowledge = knowledge
        self.knowledge_v2 = knowledge_v2_enabled() if knowledge_v2 is None else knowledge_v2
        self.extractor = extractor
        self.projection_renderer = projection_renderer
        self._knowledge_service: KnowledgeService | None = None

    @property
    def knowledge_service(self) -> KnowledgeService:
        if self.knowledge is None:
            raise StoreError("This service has no v2 knowledge store.")
        if self._knowledge_service is None:
            self._knowledge_service = KnowledgeService(
                self.knowledge,
                knowledge_space_id=self.knowledge_space_id,
                extractor=self.extractor,
                renderer=self.projection_renderer,
            )
        return self._knowledge_service

    def projects(self, actor_subject: str) -> dict[str, Any]:
        return {"projects": self.store.list_projects(), "actor_subject": actor_subject}

    def create_project(self, actor_subject: str, project_id: str, name: str) -> dict[str, Any]:
        result = self.store.create_project(project_id, name, actor_subject)
        result["actor_subject"] = actor_subject
        return result

    def status(self, actor_subject: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        if self.knowledge_v2 and self.knowledge is not None:
            result = self.knowledge_service.status(project_id)
            result["actor_subject"] = actor_subject
            return result
        snapshot = self.store.list_pages(project_id)
        return {"project_id": project_id, "version": snapshot["version"], "page_count": len(snapshot["pages"]), "actor_subject": actor_subject}

    def search(self, actor_subject: str, query: str, limit: int = 20, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        if self.knowledge_v2 and self.knowledge is not None:
            result = self.knowledge_service.search(project_id, query, limit=limit)
            result["actor_subject"] = actor_subject
            return result
        result = self.store.search_pages(query, limit, project_id)
        result["actor_subject"] = actor_subject
        return result

    def page(self, actor_subject: str, slug: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        if self.knowledge_v2 and self.knowledge is not None:
            projection = self.knowledge.projection(slug, self.knowledge_service.scope(project_id))
            if projection is not None:
                projection["actor_subject"] = actor_subject
                return projection
            redirect = self.knowledge.resolve_page_redirect(slug, self.knowledge_service.scope(project_id))
            if redirect is not None:
                redirect["actor_subject"] = actor_subject
                return redirect
        result = self.store.get_page(slug, project_id)
        result["actor_subject"] = actor_subject
        return result

    def claim(self, actor_subject: str, claim_id: str, *, version: int | None = None, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.knowledge_service.claim(project_id, claim_id, version=version)
        result["actor_subject"] = actor_subject
        return result

    def evidence(self, actor_subject: str, evidence_id: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.knowledge_service.evidence(project_id, evidence_id)
        result["actor_subject"] = actor_subject
        return result

    def explain(self, actor_subject: str, claim_id: str, *, mode: str = "why", max_depth: int = 3, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.knowledge_service.explain(project_id, claim_id, mode=mode, max_depth=max_depth)
        result["actor_subject"] = actor_subject
        return result

    def reviews(self, actor_subject: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        return {
            "reviews": self.knowledge_service.open_reviews(project_id),
            "actor_subject": actor_subject,
        }

    def review_action(
        self,
        actor_subject: str,
        review_id: str,
        expected_version: str,
        action: str,
        idempotency_key: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        note: str = "",
        edited_statement: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        result = self.knowledge_service.review(
            actor_subject=actor_subject,
            project_id=project_id,
            review_id=review_id,
            expected_version=expected_version,
            action=action,
            idempotency_key=idempotency_key,
            note=note,
            edited_statement=edited_statement,
            topic_id=topic_id,
        )
        result["actor_subject"] = actor_subject
        return result

    def ingest(
        self,
        actor_subject: str,
        base_version: int,
        idempotency_key: str,
        materials: Iterable[Any],
        purpose: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        run_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """The v2 write path: material in, claims out, one atomic commit."""

        report = self.knowledge_service.ingest(
            actor_subject=actor_subject,
            project_id=project_id,
            source_inputs=[self._source_input(material) for material in materials],
            base_version=base_version,
            idempotency_key=idempotency_key,
            purpose=purpose,
            run_id=run_id or f"run-{idempotency_key}",
            dry_run=dry_run,
        )
        result = report.as_dict()
        result["actor_subject"] = actor_subject
        return result

    @staticmethod
    def _source_input(material: Any) -> dict[str, Any]:
        if not isinstance(material, dict):
            raise StoreError("Each material must be an object.")
        return {
            "source_id": str(material.get("source_id", material.get("id", ""))),
            "kind": str(material.get("kind", "material")),
            "label": str(material.get("label", material.get("source_id", ""))),
            "content": str(material.get("content", material.get("text", ""))),
            "source_time": material.get("source_time"),
            "raw_available": bool(material.get("raw_available", True)),
        }

    def submit(
        self,
        actor_subject: str,
        base_version: int,
        idempotency_key: str,
        materials: Iterable[Any],
        purpose: str,
        *,
        update: dict[str, Any] | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> dict[str, Any]:
        """Organize against a snapshot, then commit only if its base still matches.

        With v2 writes enabled this route closes. A pre-built `pages[]` package is
        rejected outright because committing it would put page text into the wiki
        without any claim behind it, and silently converting it would commit
        knowledge the caller never reviewed. Callers are sent to `ingest`.
        """

        if self.knowledge_v2:
            if update is not None:
                raise LegacyWriteRejected(
                    "v2 knowledge writes are enabled; a pre-built pages[] update cannot be committed. "
                    "Submit material through ingest so the change goes through claims."
                )
            return self.ingest(
                actor_subject, base_version, idempotency_key, materials, purpose, project_id=project_id
            )

        material_list = list(materials)
        if update is None:
            existing_submission = self.store.submission(idempotency_key, project_id)
            if existing_submission and existing_submission["intent_hash"] is not None:
                if existing_submission["intent_hash"] != self.store.intent_hash(base_version, material_list, purpose, project_id):
                    raise IdempotencyError("Idempotency key was already used for a different request.")
                result = existing_submission["result"]
                result["actor_subject"] = actor_subject
                return result
        package = update or self._organize(material_list, purpose, project_id)
        result = self.store.commit_update(actor_subject, base_version, idempotency_key, material_list, package, purpose=purpose, project_id=project_id)
        result["actor_subject"] = actor_subject
        return result

    def _organize(self, material_list: list[Any], purpose: str, project_id: str) -> dict[str, Any]:
        """Choose a submit mode, then run it.

        `submit_mode` is the only place that decides whether a catalog is worth sending.
        An empty catalog needs no special case: `select_pages` slices an empty catalog into
        no requests, so routing a project with no pages costs no model call and falls
        through to the full submit below.
        """

        snapshot = self.store.list_pages(project_id)
        catalog = self.store.list_page_catalog(project_id)
        if submit_mode(len(catalog["entries"]), snapshot_chars(snapshot["pages"])) != ROUTED:
            return self.core.organize(material_list, snapshot["pages"], purpose)
        routed = run_routed(
            self.core,
            catalog["entries"],
            lambda slugs: self.store.get_pages_by_slugs(slugs, project_id)["pages"],
            material_list,
            purpose,
        )
        if routed is not None:
            return routed
        # Routing selected nothing, which may simply mean the evidence describes pages that
        # do not exist yet. A full submit can decide that; committing an empty package would
        # turn a routing miss into a silent no-op.
        return self.core.organize(material_list, snapshot["pages"], purpose)

    def versions(self, actor_subject: str, slug: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.store.page_versions(slug, project_id)
        result["actor_subject"] = actor_subject
        return result

    def restore(self, actor_subject: str, slug: str, version_id: int, base_version: int, idempotency_key: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        """Restore a historical page, or refuse to under v2.

        Restoring page text would put a superseded wording back on the page
        without touching the claim that replaced it, so the page and the knowledge
        would disagree and the page would win by being read first. Under v2 the
        historical wording goes back in as material through `restore_as_manual_note`
        and earns its place through a review action.
        """

        if self.knowledge_v2:
            raise LegacyWriteRejected(
                "v2 knowledge writes are enabled; restoring an old page does not restore the decision it recorded. "
                "Use restore_as_manual_note so the historical wording is re-ingested as material."
            )
        result = self.store.restore_page(actor_subject, slug, version_id, base_version, idempotency_key, project_id)
        result["actor_subject"] = actor_subject
        return result

    def restore_as_manual_note(
        self,
        actor_subject: str,
        slug: str,
        version_id: int,
        base_version: int,
        idempotency_key: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        note: str = "",
    ) -> dict[str, Any]:
        """Re-ingest a historical page body as material, marked as retrospective.

        The wording carries the time it is being re-submitted at, not the time it
        described. Labelling it a retrospective statement is what keeps a
        confirmation given today from being read as a reason recorded back then.
        """

        historical = self.store.page_versions(slug, project_id)["versions"]
        match = next((item for item in historical if item["id"] == version_id), None)
        if match is None:
            raise StoreError("Historical page version does not exist.")
        statement = (
            f"[retrospective statement submitted {_today()} about page {slug} version {version_id}]\n"
            f"{match['body']}"
        )
        return self.ingest(
            actor_subject,
            base_version,
            idempotency_key,
            [
                {
                    "source_id": f"manual-note:{slug}:v{version_id}",
                    "kind": "manual_note",
                    "label": f"{slug} v{version_id} (retrospective)",
                    "content": statement,
                    "source_time": match["created_at"],
                }
            ],
            note or f"Retrospective re-submission of {slug} version {version_id}.",
            project_id=project_id,
        )


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()
