"""Company Wiki use cases built on the authenticated subject and atomic store."""

from __future__ import annotations

from typing import Any, Iterable

from .core import Model, WikiCore
from .model import configured_model
from .store import DEFAULT_PROJECT_ID, IdempotencyError, SharedWikiStore


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
        snapshot = self.store.list_pages(project_id)
        package = update or self.core.organize(material_list, snapshot["pages"], purpose)
        result = self.store.commit_update(actor_subject, base_version, idempotency_key, material_list, package, purpose=purpose, project_id=project_id)
        result["actor_subject"] = actor_subject
        return result

    def versions(self, actor_subject: str, slug: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.store.page_versions(slug, project_id)
        result["actor_subject"] = actor_subject
        return result

    def restore(self, actor_subject: str, slug: str, version_id: int, base_version: int, idempotency_key: str, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        result = self.store.restore_page(actor_subject, slug, version_id, base_version, idempotency_key, project_id)
        result["actor_subject"] = actor_subject
        return result
