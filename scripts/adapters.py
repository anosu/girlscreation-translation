"""Load trusted game modules through the shared adapter contract."""

import importlib
from pathlib import Path
from typing import Protocol, cast

from scripts.models import Catalog


class GameAdapter(Protocol):
    """The source and publication operations implemented by each game module."""

    def settings(self, options: dict) -> dict: ...

    def fetch(
        self, cache: Path, selection: list[str] | None, options: dict
    ) -> dict: ...

    def extract(self, cache: Path, options: dict) -> Catalog: ...

    def publication(
        self, catalog: Catalog, translations: Path, options: dict
    ) -> Catalog: ...

    def context(self, task: dict, cache: Path, options: dict) -> dict: ...


def load_adapter(module: str) -> GameAdapter:
    """Import an explicitly configured local adapter; never install remote code."""
    adapter = importlib.import_module(module)
    for name in ("fetch", "extract", "context", "publication", "settings"):
        if not callable(getattr(adapter, name, None)):
            raise ValueError(f"Adapter {module} must implement {name}()")
    return cast(GameAdapter, adapter)
