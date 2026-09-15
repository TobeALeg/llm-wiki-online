"""Remote request orchestration with an explicit local-only mode."""

from __future__ import annotations

from typing import Any, Iterable

from .core import Model, WikiCore
from .model import configured_model


class RemoteWikiService:
    def __init__(self, model: Model | None = None):
        self.core = WikiCore(model or configured_model())

    def organize_local(
        self,
        materials: Iterable[Any],
        existing_pages: Iterable[Any],
        purpose: str,
    ) -> dict[str, Any]:
        """Return a package only; this method deliberately has no storage collaborator."""

        return self.core.organize(materials, existing_pages, purpose)
