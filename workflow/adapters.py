"""One adapter entry point for acquisition and extraction, with no model access."""

import importlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from workflow.resources import Resource


@dataclass(frozen=True)
class CollectRequest:
    root: Path
    options: dict
    cache: Path
    selection: list[str] | None
    translations: dict[str, Path]


class GameAdapter(Protocol):
    def collect(self, request: CollectRequest) -> Iterable[Resource]: ...


def load_adapter(module: str) -> GameAdapter:
    module = "adapters.json_file" if module == "json" else module
    try:
        adapter = importlib.import_module(module)
    except ImportError as error:
        raise ValueError(f"Cannot load adapter {module}: {error}") from error
    if not callable(getattr(adapter, "collect", None)):
        raise ValueError(f"Adapter {module} must implement collect(request)")
    return cast(GameAdapter, adapter)
