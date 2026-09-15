"""Startup and periodic reconciliation of the Menti member directory."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any

from .auth import AuthService


class MemberReconciler:
    def __init__(self, auth: AuthService, fetch_members: Callable[[], Iterable[dict[str, Any]]], *, interval_seconds: int = 900):
        self.auth = auth
        self.fetch_members = fetch_members
        self.interval_seconds = max(1, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def reconcile_once(self) -> int:
        return self.auth.reconcile_members(self.fetch_members())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.reconcile_once()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="llm-wiki-member-reconcile", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.reconcile_once()
            except Exception:
                # A transient directory outage must not kill future reconciliation.
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
