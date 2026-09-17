"""Company Wiki use cases built on the authenticated subject and atomic store."""

from __future__ import annotations

from typing import Any, Iterable

from .core import ROUTED, Model, WikiCore, snapshot_chars, submit_mode
from .model import configured_model
from .store import DEFAULT_PROJECT_ID, IdempotencyError, SharedWikiStore
from .wiki_pipeline import run_routed


class SharedWikiService:
    def __init__(self, store: SharedWikiStore, model: Model | None = None):
        self.store = store
        self.core = WikiCore(model or configured_model())

    def projects(self, actor_subject: str) -> dict[str, Any]:
        return {"projects": self.store.list_projects(), "actor_subject": actor_subject}

    def create_project(self, actor_subject: str, project_id: str, name: str) -> dict[str, Any]:
        result = self.store.create_project(project_id, name, actor_subject)
        result["actor_subject"] = actor_subject
        return result

    def status(self, actor_subject: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        snapshot = self.store.list_pages(project_id)
        return {"project_id": project_id, "version": snapshot["version"], "page_count": len(snapshot["pages"]), "actor_subject": actor_subject}

    def search(self, actor_subject: str, query: str, limit: int = 20, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.store.search_pages(query, limit, project_id)
        result["actor_subject"] = actor_subject
        return result

    def page(self, actor_subject: str, slug: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.store.get_page(slug, project_id)
        result["actor_subject"] = actor_subject
        return result

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
        """Organize against a snapshot, then commit only if its base still matches."""

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
        result = self.store.restore_page(actor_subject, slug, version_id, base_version, idempotency_key, project_id)
        result["actor_subject"] = actor_subject
        return result
