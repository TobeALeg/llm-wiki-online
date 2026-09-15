"""Company Wiki use cases built on the authenticated subject and atomic store."""

from __future__ import annotations

from typing import Any, Iterable

from .core import Model, WikiCore
from .model import configured_model
from .store import SharedWikiStore


class SharedWikiService:
    def __init__(self, store: SharedWikiStore, model: Model | None = None):
        self.store = store
        self.core = WikiCore(model or configured_model())

    def status(self, actor_subject: str) -> dict[str, Any]:
        snapshot = self.store.list_pages()
        return {"version": snapshot["version"], "page_count": len(snapshot["pages"]), "actor_subject": actor_subject}

    def search(self, actor_subject: str, query: str, limit: int = 20) -> dict[str, Any]:
        result = self.store.search_pages(query, limit)
        result["actor_subject"] = actor_subject
        return result

    def page(self, actor_subject: str, slug: str) -> dict[str, Any]:
        result = self.store.get_page(slug)
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
    ) -> dict[str, Any]:
        """Organize against a snapshot, then commit only if its base still matches."""

        material_list = list(materials)
        snapshot = self.store.list_pages()
        if base_version != snapshot["version"]:
            from .store import ConflictError

            raise ConflictError(
                f"Wiki changed since base_version {base_version}; retry from version {snapshot['version']}.",
                current_version=snapshot["version"],
            )
        package = update or self.core.organize(material_list, snapshot["pages"], purpose)
        result = self.store.commit_update(actor_subject, base_version, idempotency_key, material_list, package)
        result["actor_subject"] = actor_subject
        return result

    def versions(self, actor_subject: str, slug: str) -> dict[str, Any]:
        result = self.store.page_versions(slug)
        result["actor_subject"] = actor_subject
        return result

    def restore(self, actor_subject: str, slug: str, version_id: int, base_version: int, idempotency_key: str) -> dict[str, Any]:
        result = self.store.restore_page(actor_subject, slug, version_id, base_version, idempotency_key)
        result["actor_subject"] = actor_subject
        return result
