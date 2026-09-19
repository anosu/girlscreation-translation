"""Girls Creation acquisition and extraction through the resource adapter contract."""

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import Field

from workflow.adapters import CollectRequest
from workflow.config import StrictModel, Text
from workflow.resources import Resource, TextBlock
from workflow.utils import read_json


class Settings(StrictModel):
    master_fields: Text = "master-fields.json"
    source_index: int = Field(default=0, ge=0)
    check_existing: bool = False
    # Optional parsed source directory for offline imports and old-cache migration.
    input: Text | None = None


def source_text(value: Any, kind: str, index: int = 0) -> str | None:
    """Extract one original-language value without changing its source key."""
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"Expected a language array: {value!r}")
        value = value[index] if len(value) > index else None
    elif kind == "strings":
        if not isinstance(value, str):
            raise ValueError(f"Expected a pipe-separated string: {value!r}")
        parts = value.split("|")
        value = parts[index] if len(parts) > index else None
    elif kind != "string":
        raise ValueError(f"Unknown master field type: {kind}")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Expected text: {value!r}")
    return value if value and value.strip() else None


def select_novels(
    novel_ids: Iterable[str],
    translations: list[Path] | None,
    check_existing: bool = False,
) -> set[str]:
    """Skip a story only when every selected language already has its output file."""
    return {
        novel_id
        for novel_id in novel_ids
        if check_existing
        or translations is None
        or any(
            not (root / "novels" / f"{novel_id}.json").is_file()
            for root in translations
        )
    }


def collect(request: CollectRequest) -> Iterable[Resource]:
    options = Settings.model_validate(request.options)
    check_existing = options.check_existing
    override = os.environ.get("GIRLSCREATION_CHECK_EXISTING")
    if override:
        if override not in {"true", "false"}:
            raise ValueError("GIRLSCREATION_CHECK_EXISTING must be true or false")
        check_existing = override == "true"
    if request.selection and any(
        not re.fullmatch(r"[0-9]{5,8}", value) for value in request.selection
    ):
        raise ValueError("Girls Creation source IDs must be numeric novel IDs")
    translations = list(request.translations.values())
    if options.input:
        cache = (request.root / options.input).resolve()
        if (cache / ".fetching").exists():
            raise ValueError(
                "Source acquisition was interrupted; complete sync before importing this cache"
            )
        index = read_json(cache / "index.json")
    else:
        from adapters.girlscreation.fetch import fetch_sources

        cache = request.cache
        index = fetch_sources(
            cache,
            request.selection,
            translations=translations,
            check_existing=check_existing,
        )
    available = set(index["novels"])
    if any(not re.fullmatch(r"[0-9]{5,8}", value) for value in available):
        raise ValueError("Invalid novel ID in source index")
    if request.selection and not set(request.selection) <= available:
        raise ValueError("Selected novel IDs are missing from the source index")
    selected = select_novels(
        request.selection if request.selection is not None else available,
        translations,
        check_existing or request.selection is not None,
    )
    if not selected <= set(index.get("fetched_novels", available)):
        raise ValueError(
            "Required novels were not fetched; run sync with the same selection and targets"
        )

    resources = []
    names: dict[str, None] = {}
    for novel_id in sorted(selected):
        messages = read_json(cache / "novels" / f"{novel_id}.json")["messages"]
        titles = list(
            dict.fromkeys(m["message"] for m in messages if m["kind"] == "title")
        )
        lines = []
        for message in messages:
            if message["kind"] != "message":
                continue
            speaker = message["name"] or None
            if speaker:
                names[speaker] = None
            lines.append([speaker, message["message"]])
        output = f"novels/{novel_id}.json"
        context = {"title": titles[0]} if titles else {}
        if lines:
            resources.append(
                Resource(
                    id=f"novels/{novel_id}",
                    output=output,
                    kind="dialogue",
                    lines=lines,
                    context=context,
                )
            )
        # GCMod also replaces script title commands through the same novel dictionary.
        if titles:
            resources.append(
                Resource(
                    id=f"novels/{novel_id}/titles",
                    output=output,
                    kind="text",
                    blocks=[TextBlock(texts=titles)],
                    context=context,
                )
            )
    if names:
        yield Resource(
            id="names",
            output="names.json",
            kind="text",
            blocks=[TextBlock(texts=list(names))],
        )
    yield from resources

    master = read_json(cache / "master.json")
    schema = read_json(request.root / options.master_fields)
    suffixes = {"string": "", "strings": "|", "array": "[]"}
    for table, fields in schema.items():
        if table not in master:
            raise ValueError(f"Configured master table is missing: {table}")
        for field, kind in fields.items():
            if kind not in suffixes:
                raise ValueError(f"Unknown master field type: {kind}")
            texts: dict[str, None] = {}
            for row in master[table]:
                if field not in row:
                    raise ValueError(f"Missing master field: {table}.{field}")
                source = source_text(row[field], kind, options.source_index)
                if source is not None:
                    texts[source] = None
            if texts:
                yield Resource(
                    output="master.json",
                    path=[table, field + suffixes[kind]],
                    kind="text",
                    blocks=[TextBlock(texts=list(texts))],
                )
