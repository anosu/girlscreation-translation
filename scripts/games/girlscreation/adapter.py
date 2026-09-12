"""Map Girls Creation's assets into the framework's JSON translation entries."""

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import Field

from scripts.config import StrictModel, Text
from scripts.models import Binding, Catalog, TermBinding
from scripts.utils import digest, directory_digest, read_json
from scripts.validate import PROTECTED

TITLE_TABLES = {"mNovels", "mIntermissionNovels"}


class Settings(StrictModel):
    master_fields: Text
    source_index: int = Field(default=0, ge=0)


def settings(options: dict) -> dict:
    return Settings.model_validate(options).model_dump()


def source_text(value: Any, kind: str, index: int = 0) -> str | None:
    """Extract the configured original-language column from a master field."""
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
    return value or None


def fetch(
    cache: Path,
    selection: list[str] | None,
    options: dict,
    *,
    translations: list[Path] | None = None,
    check_existing: bool = False,
) -> dict:
    """Refresh game snapshots once for all target languages."""
    from scripts.games.girlscreation.fetch import fetch_sources

    return fetch_sources(
        cache, selection, translations=translations, check_existing=check_existing
    )


def select_novels(
    novel_ids: Iterable[str],
    translations: list[Path] | None,
    check_existing: bool = False,
) -> set[str]:
    """A published novel is skipped unless an existing-content check was requested."""
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


def extract(
    cache: Path,
    options: dict,
    *,
    translations: list[Path] | None = None,
    check_existing: bool = False,
) -> Catalog:
    """Emit source entries; let the core handle language-specific reuse and diffs."""
    index = read_json(cache / "index.json")
    selected = select_novels(
        index["novels"], translations, check_existing or index.get("partial", False)
    )
    if not selected <= set(index.get("fetched_novels", index["novels"])):
        raise ValueError(
            "Required novels were not fetched; rerun fetch with the same targets and check-existing mode"
        )
    master = read_json(cache / "master.json")
    schema = read_json(Path(options["root"]) / options["master_fields"])
    entries = []
    titles, title_context, names = (
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )

    def target(file: str, path: list[str], priority: int = 0) -> dict:
        return {"file": file, "path": path, "priority": priority, "track_source": False}

    def entry(
        kind: str, group: str, source: str, targets: list[dict], context: Any, **extra
    ) -> None:
        entries.append(
            {
                "id": digest([kind, targets[0]["file"], targets[0]["path"]]),
                "category": kind,
                "group": group,
                "source": source,
                "targets": targets,
                "context": context,
                "term": kind == "names",
                "rules": {
                    "preserve_tags": True,
                    "preserve_newlines": True,
                    "protected_patterns": [PROTECTED.pattern],
                    "name_kinds": ["names"],
                    "number_kinds": ["master"],
                },
                **extra,
            }
        )

    for novel_id in sorted(selected):
        messages = read_json(cache / "novels" / f"{novel_id}.json")["messages"]
        title = next((m["message"] for m in messages if m["kind"] == "title"), "")
        repeated = defaultdict(list)
        for message in messages:
            source = message["message"]
            context = {
                "novel_id": novel_id,
                "title": title,
                "line": message["line"],
                "speaker": message["name"],
            }
            if message["name"]:
                names[message["name"]].append(context)
            if message["kind"] == "title":
                titles[source].append(target(f"novels/{novel_id}.json", [source]))
                title_context[source].append({"novel_id": novel_id, "kind": "title"})
            else:
                repeated[source].append(context)
        for source, contexts in repeated.items():
            entry(
                "novels",
                f"novels-{novel_id}",
                source,
                [target(f"novels/{novel_id}.json", [source])],
                contexts,
            )
    for source, contexts in sorted(names.items()):
        entry(
            "names",
            "names",
            source,
            [target("names.json", [source])],
            contexts[:5],
            use_terms=True,
        )
    for table, fields in schema.items():
        if table not in master:
            raise ValueError(f"Configured master table is missing: {table}")
        for field, field_type in fields.items():
            output_field = (
                field + {"string": "", "strings": "|", "array": "[]"}[field_type]
            )
            occurrences = defaultdict(list)
            for row in master[table]:
                if field not in row:
                    raise ValueError(f"Missing master field: {table}.{field}")
                source = source_text(
                    row[field], field_type, options.get("source_index", 0)
                )
                if not source:
                    continue
                row_id = row.get("id", row.get("series_no"))
                if table in TITLE_TABLES:
                    titles[source].append(
                        target("master.json", [table, output_field, source], 100)
                    )
                    title_context[source].append(
                        {"table": table, "field": field, "record_id": row_id}
                    )
                else:
                    occurrences[source].append(row_id)
            for source, ids in occurrences.items():
                entry(
                    "master",
                    f"master-{table}",
                    source,
                    [target("master.json", [table, output_field, source])],
                    {"table": table, "field": field, "record_ids": ids[:10]},
                    use_terms=True,
                )
    for source, targets in sorted(titles.items()):
        stories = sorted(t["file"] for t in targets if t["file"].startswith("novels/"))
        group = "novels-" + Path(stories[0]).stem if stories else "novels-titles"
        entry(
            "novels",
            group,
            source,
            targets,
            {"kind": "title", "references": title_context[source]},
            reconcile=True,
        )
    for item in entries:
        references = {"master.json"} if item["category"] == "master" else set()
        if any(t["file"] == "master.json" for t in item["targets"]):
            references.add("master.json")
        contexts = (
            item["context"]
            if isinstance(item["context"], list)
            else item["context"].get("references", [])
        )
        references.update(
            f"novels/{c['novel_id']}.json" for c in contexts if "novel_id" in c
        )
        references.update(
            t["file"] for t in item["targets"] if t["file"].startswith("novels/")
        )
        item["references"] = sorted(references)
    versions = {}
    for item in entries:
        for reference in item["references"]:
            if reference not in versions:
                path = cache / reference
                versions[reference] = (
                    directory_digest(cache, [reference]) if path.exists() else None
                )
        item["context_version"] = digest(
            [item["context"], {r: versions[r] for r in item["references"]}]
        )
        if (
            item["category"] == "master"
            and item["context"]["table"] in {"mUnits", "mSubunits"}
            and item["context"]["field"] == "ml_name"
        ):
            item["term"] = True
    return Catalog.model_validate(
        {
            "entries": entries,
            "complete": not index.get("partial", False)
            and selected == set(index["novels"]),
            "source_files": [
                "index.json",
                "master.json",
                *[f"novels/{novel_id}.json" for novel_id in sorted(selected)],
            ],
            "atomic_files": [
                f"novels/{novel_id}.json" for novel_id in sorted(selected)
            ],
        }
    )


def publication(catalog: Catalog, translations: Path, options: dict) -> Catalog:
    """Resolve existing title locations and normalize terminology for one target."""
    names_path, master_path = translations / "names.json", translations / "master.json"
    names = read_json(names_path) if names_path.exists() else {}
    master = read_json(master_path) if master_path.exists() else {}
    bindings = []
    for source in names:
        bindings.append(
            {
                "source": source,
                "reference": source,
                "target": {
                    "file": "names.json",
                    "path": [source],
                    "track_source": False,
                },
            }
        )
    for table in ("mUnits", "mSubunits"):
        for source in master.get(table, {}).get("ml_name[]", {}):
            bindings.append(
                {
                    "source": source,
                    "target": {
                        "file": "master.json",
                        "path": [table, "ml_name[]", source],
                        "track_source": False,
                    },
                }
            )
    entries = []
    historical: dict[str, dict] = {"master.json": master, "names.json": names}

    def populated(binding: Binding) -> bool:
        if binding.file not in historical:
            path = translations / binding.file
            historical[binding.file] = read_json(path) if path.exists() else {}
        value = historical[binding.file]
        for key in binding.path:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        return isinstance(value, str) and bool(value.strip())

    for item in catalog.entries:
        if item.term:
            bindings.append(
                {
                    "source": item.source,
                    "reference": item.source if item.category == "names" else None,
                    "target": item.targets[0].model_dump(),
                }
            )
        targets = list(item.targets)
        if (
            item.reconcile
            and isinstance(item.context, dict)
            and (catalog.check_existing or any(not populated(t) for t in targets))
        ):
            for record in item.context.get("references", []):
                if record.get("table") == "mNovels" and re.fullmatch(
                    r"\d+", str(record["record_id"])
                ):
                    file = f"novels/{record['record_id']}.json"
                    if any(t.file == file and t.path == [item.source] for t in targets):
                        continue
                    path = translations / file
                    if file not in historical:
                        historical[file] = read_json(path) if path.exists() else {}
                    if item.source in historical[file]:
                        targets.append(
                            Binding(
                                file=file,
                                path=[item.source],
                                priority=0,
                                track_source=False,
                            )
                        )
        entries.append(
            item.model_copy(update={"targets": targets})
            if targets != item.targets
            else item
        )
    return catalog.model_copy(
        update={
            "entries": entries,
            "term_bindings": [
                TermBinding.model_validate(binding) for binding in bindings
            ],
        }
    )


def context(task: dict, cache: Path, options: dict) -> dict:
    """Supply complete stories and relevant raw master records to the agent."""
    result = {"task": task, "stories": {}, "master_records": []}
    for reference in task.get("references", []):
        path = cache / reference
        if reference.startswith("novels/") and path.exists():
            result["stories"][path.stem] = read_json(path)
    if task["category"] == "master":
        info = task["context"]
        rows = read_json(cache / "master.json")[info["table"]]
        result["master_records"] = [
            r
            for r in rows
            if r.get("id", r.get("series_no")) in info.get("record_ids", [])
        ][:10]
    return result
