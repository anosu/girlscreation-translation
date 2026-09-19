"""Shared filesystem and JSON-object addressing for translation dictionaries."""

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from workflow.utils import read_json


def output_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or ":" in value
        or any(part.startswith(".") for part in path.parts)
        or path.as_posix() != value
        or not value.endswith(".json")
        or path.name == "manifest.json"
    ):
        raise ValueError(f"Invalid output dictionary path: {value}")
    return value


def namespace_path(value: list[str]) -> list[str]:
    if any(not key.strip() for key in value):
        raise ValueError("Dictionary paths must contain nonblank object keys")
    return value


def validate_document(data: object, location: object = "document") -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"Expected translation dictionaries: {location}")
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"Blank or invalid dictionary key: {location}")
        if isinstance(value, dict):
            validate_document(value, location)
        elif not isinstance(value, str):
            raise ValueError(
                f"Expected nested dictionaries or translation strings: {location}"
            )
    return data


def read_document(path: Path) -> dict:
    return validate_document(read_json(path), path) if path.exists() else {}


def dictionary_at(
    document: dict, path: list[str], *, create: bool = False
) -> dict[str, str]:
    """Return a flat leaf dictionary. Never overwrite a string with an object."""
    namespace_path(path)
    node = document
    for key in path:
        if not isinstance(node, dict):
            raise ValueError(f"Dictionary path crosses a translation string: {path}")
        if key not in node:
            if not create:
                return {}
            node[key] = {}
        node = node[key]
    if not isinstance(node, dict) or any(
        not isinstance(value, str) for value in node.values()
    ):
        raise ValueError(f"Expected a flat source: translation dictionary at {path}")
    return node


def read_dictionary(file: Path, path: list[str] | None = None) -> dict[str, str]:
    return dictionary_at(read_document(file), path or [])


def validate_locations(locations: Iterable[tuple[str, list[str]]]) -> None:
    """One JSON location cannot simultaneously be a leaf dictionary and a namespace."""
    by_file: dict[str, set[tuple[str, ...]]] = {}
    for file, path in locations:
        by_file.setdefault(output_path(file), set()).add(tuple(namespace_path(path)))
    for file, paths in by_file.items():
        ordered = sorted(paths)
        for parent, child in zip(ordered, ordered[1:]):
            if child[: len(parent)] == parent:
                raise ValueError(
                    f"Overlapping dictionary paths in {file}: {list(parent)} and {list(child)}"
                )
