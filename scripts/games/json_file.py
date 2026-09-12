"""Portable adapter for games exporting normalized source entries as JSON."""

from pathlib import Path

from scripts.config import StrictModel, Text
from scripts.models import Catalog, TermBinding
from scripts.utils import digest, read_json, write_json


class Settings(StrictModel):
    input: Text


def settings(options: dict) -> dict:
    return Settings.model_validate(options).model_dump()


def context_entries(entries: list[dict]) -> list[dict]:
    """Publication addresses do not describe the meaning of a scene."""
    return [
        {
            key: value
            for key, value in entry.items()
            if key not in {"targets", "references", "context_version", "reconcile"}
        }
        for entry in entries
    ]


def group_path(cache: Path, group: str) -> Path:
    return cache / "groups" / f"{digest(group)}.json"


def fetch(
    cache: Path,
    selection: list[str] | None,
    options: dict,
    *,
    translations: list[Path] | None = None,
    check_existing: bool = False,
) -> dict:
    """Import a local source export without requiring UnityPy or a game server."""
    path = Path(options["root"]) / options["input"]
    entries = read_json(path)
    if not isinstance(entries, list):
        raise ValueError("The JSON source export must be an array of entries")
    if selection:
        entries = [e for e in entries if e.get("group") in selection]
    cache.mkdir(parents=True, exist_ok=True)
    (cache / ".fetching").write_text("Importing source snapshot\n", encoding="utf-8")
    write_json(cache / "entries.json", entries)
    groups = {}
    for entry in entries:
        groups.setdefault(entry["group"], []).append(entry)
    for group, items in groups.items():
        write_json(group_path(cache, group), context_entries(items))
    index = {
        "version": 1,
        "hash": digest(entries),
        "entries": len(entries),
        "partial": bool(selection),
    }
    write_json(cache / "index.json", index)
    (cache / ".fetching").unlink()
    return index


def extract(
    cache: Path,
    options: dict,
    *,
    translations: list[Path] | None = None,
    check_existing: bool = False,
) -> Catalog:
    """Return normalized entries, including their arbitrary JSON publication keys."""
    entries = read_json(cache / "entries.json")
    groups = {}
    for entry in entries:
        groups.setdefault(entry["group"], []).append(entry)
    versions = {
        group: digest(context_entries(items)) for group, items in groups.items()
    }
    return Catalog.model_validate(
        {
            "complete": not read_json(cache / "index.json").get("partial", False),
            "entries": [
                {
                    **entry,
                    "references": [
                        group_path(cache, entry["group"]).relative_to(cache).as_posix()
                    ],
                    "context_version": versions[entry["group"]],
                }
                for entry in entries
            ],
            "source_files": [
                "index.json",
                "entries.json",
                *[
                    group_path(cache, group).relative_to(cache).as_posix()
                    for group in groups
                ],
            ],
        }
    )


def publication(catalog: Catalog, translations: Path, options: dict) -> Catalog:
    entries = read_json(Path(options["root"]) / options["input"])
    bindings = [
        {
            "source": entry["source"],
            "reference": entry["source"],
            "target": target,
        }
        for entry in entries
        if entry.get("term")
        for target in entry["targets"]
    ]
    return catalog.model_copy(
        update={
            "term_bindings": [
                TermBinding.model_validate(binding) for binding in bindings
            ]
        }
    )


def context(task: dict, cache: Path, options: dict) -> dict:
    """Return every source entry in the scene or logical group, preserving order."""
    return {
        "task": task,
        "group": read_json(group_path(cache, task["group"])),
    }
