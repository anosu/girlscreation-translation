"""Plan one target from a shared source catalog and its current publication."""

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from scripts.adapters import load_adapter
from scripts.config import Project, Target
from scripts.glossary import (
    project_terms,
    read_optional,
    resolve_glossary,
    term_for,
)
from scripts.models import Catalog, CompiledCatalog, Plan
from scripts.utils import digest, directory_digest, read_json, write_bytes, write_json
from scripts.validate import combine_rules


def translation_at(document: dict, path: list[str]) -> str | None:
    value = document
    for key in path:
        if not isinstance(value, dict):
            raise ValueError(f"Invalid translation path: {path}")
        value = value.get(key)
        if value is None:
            return None
    if not isinstance(value, str):
        raise ValueError(f"Translation must be a string: {path}")
    return value


def output_paths(document: dict, prefix: tuple = ()):
    for key, value in document.items():
        if isinstance(value, dict):
            yield from output_paths(value, (*prefix, key))
        else:
            yield (*prefix, key)


def published_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.json")
        if p.name != "manifest.json"
    )


def bind_runtime(work: Path, project: Project, target: Target) -> None:
    """Rebind portable artifacts to this checkout; locations never enter content IDs."""

    def location(path: Path) -> str:
        try:
            return Path(os.path.relpath(path, work)).as_posix()
        except ValueError:
            # A Windows cross-volume address is local runtime data, not portable content.
            return str(path.resolve())

    write_json(
        work / "runtime.json",
        {
            key: location(path)
            for key, path in {
                "project_config": project.config,
                "translations": target.translations,
                "sources": project.sources,
                "glossary": target.glossary,
            }.items()
        },
    )


def runtime_paths(work: Path) -> dict[str, Path]:
    return {
        key: (work / value).resolve()
        for key, value in read_json(work / "runtime.json").items()
    }


def read_plan(work: Path) -> Plan:
    plan = Plan.model_validate(read_json(work / "plan.json"))
    if plan.id != digest(plan.model_dump(exclude={"id"})):
        raise ValueError("Plan contents changed; prepare again")
    return plan


def catalog_inputs(project: Project) -> str:
    def fingerprint(path: Path) -> str:
        if path.suffix == ".json":
            return digest(read_json(path))
        content = (
            path.read_text(encoding="utf-8").encode("utf-8")
            if path.suffix == ".py"
            else path.read_bytes()
        )
        return hashlib.sha256(content).hexdigest()

    adapter = load_adapter(project.adapter)
    files = {"adapter": Path(getattr(adapter, "__file__"))}
    for key, value in adapter.settings(project.options).items():
        if isinstance(value, str) and (project.root / value).is_file():
            files[key] = project.root / value
    return digest(
        {
            "adapter": project.adapter,
            "options": project.options,
            "files": {key: fingerprint(path) for key, path in files.items()},
        }
    )


def source_catalog(
    project: Project,
    compiled: Path | None = None,
    *,
    targets: list[Target] | None = None,
    check_existing: bool = False,
) -> Catalog:
    if (project.sources / ".fetching").exists():
        raise ValueError(
            "Source fetch was interrupted; rerun fetch before preparing tasks"
        )
    adapter = load_adapter(project.adapter)
    targets = targets if targets is not None else project.select()
    files = {
        target.code: digest(published_files(target.translations)) for target in targets
    }
    inputs = catalog_inputs(project)
    if compiled is not None:
        artifact = CompiledCatalog.model_validate(read_json(compiled))
        if (
            artifact.project != project.id
            or artifact.inputs != inputs
            or artifact.catalog.source_version
            != directory_digest(project.sources, artifact.catalog.source_files)
            or artifact.catalog.check_existing != check_existing
            or any(
                artifact.catalog.target_files.get(code) != fingerprint
                for code, fingerprint in files.items()
            )
        ):
            raise ValueError("Shared catalog is stale; run catalog again")
        return artifact.catalog
    options = {**adapter.settings(project.options), "root": str(project.root)}
    catalog = adapter.extract(
        project.sources,
        options,
        translations=[target.translations for target in targets],
        check_existing=check_existing,
    )
    catalog.check_existing = check_existing
    catalog.target_files = files
    catalog.source_version = directory_digest(project.sources, catalog.source_files)
    return catalog


def select_tasks(
    tasks: list[dict],
    limit: int | None,
    atomic_files: set[str],
    blocked_files: set[str],
) -> list[dict]:
    """Keep new files complete, including tasks shared by more than one file."""
    by_file: dict[str, set[int]] = {file: set() for file in atomic_files}
    for number, task in enumerate(tasks):
        for binding in task["targets"]:
            if binding["file"] in by_file:
                by_file[binding["file"]].add(number)
    pending = set(range(len(tasks)))
    chosen: set[int] = set()
    required_sizes = []
    for number in range(len(tasks)):
        if number not in pending:
            continue
        group, stack = set(), [number]
        files = set()
        while stack:
            current = stack.pop()
            if current in group:
                continue
            group.add(current)
            for binding in tasks[current]["targets"]:
                file = binding["file"]
                if file in by_file:
                    files.add(file)
                    stack.extend(by_file[file] - group)
        pending -= group
        if files & blocked_files:
            blocked_files.update(files)
            continue
        required_sizes.append(len(group))
        if limit is None or len(chosen) + len(group) <= limit:
            chosen.update(group)
    if not chosen and required_sizes and limit is not None:
        raise ValueError(
            f"Task limit {limit} cannot fit a complete new file; use at least {min(required_sizes)} or omit --limit"
        )
    return [task for number, task in enumerate(tasks) if number in chosen]


def compile_catalog(
    project: Project,
    *,
    targets: list[Target] | None = None,
    check_existing: bool = False,
    export: bool = False,
) -> Catalog:
    catalog = source_catalog(project, targets=targets, check_existing=check_existing)
    write_bytes(
        project.catalog,
        json.dumps(
            CompiledCatalog(
                project=project.id, inputs=catalog_inputs(project), catalog=catalog
            ).model_dump(exclude_defaults=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    if export:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for file in catalog.source_files or []:
                bundle.write(project.sources / file, file)
        write_bytes(project.source_bundle, archive.getvalue())
    return catalog


def prepare_tasks(
    project: Project,
    target: Target,
    catalog: Catalog | None = None,
    limit: int | None = None,
    *,
    check_existing: bool = False,
) -> Plan:
    """Preserve published text, select missing translations, and plan terms first."""
    if limit is not None and limit <= 0:
        raise ValueError("Task limit must be greater than zero")
    old_plan = target.work / "plan.json"
    if old_plan.exists() and read_json(old_plan).get("version") != 7:
        raise ValueError(
            "This work directory contains legacy artifacts; configure a new work directory. The old cache has been preserved."
        )
    adapter = load_adapter(project.adapter)
    options = adapter.settings(project.options)
    catalog = adapter.publication(
        catalog
        or source_catalog(project, targets=[target], check_existing=check_existing),
        target.translations,
        {**options, "root": str(project.root)},
    )
    if catalog.target_files and catalog.target_files.get(target.code) != digest(
        published_files(target.translations)
    ):
        raise ValueError(
            "Shared catalog was prepared for another target or publication file set"
        )
    if catalog.check_existing != check_existing and catalog.target_files:
        raise ValueError("Shared catalog uses a different check-existing mode")
    identity = {
        "project": project.id,
        "language": target.code,
        "source_language": project.source_language,
    }
    term_bindings = list(
        {digest(b.model_dump()): b for b in catalog.term_bindings}.values()
    )
    documents: dict[str, dict] = {}

    def document(file: str) -> dict:
        if file not in documents:
            location = target.translations / file
            if not location.resolve().is_relative_to(target.translations.resolve()):
                raise ValueError(f"Publication path escapes target: {file}")
            documents[file] = read_optional(location)
        return documents[file]

    for binding in term_bindings:
        document(binding.target.file)
    names, terms = project_terms(
        target.translations, term_bindings, documents=documents
    )
    glossary = resolve_glossary(target.glossary, names, terms)
    tasks, claims = [], {}
    conflicts = []
    atomic_files = {
        file
        for file in catalog.atomic_files
        if not (target.translations / file).exists()
    }
    blocked_files: set[str] = set()
    for entry in catalog.entries:
        bindings = []
        for binding in entry.targets:
            raw = binding.model_dump()
            file = binding.file
            claim = (file, tuple(binding.path))
            if claim in claims and claims[claim] != entry.id:
                raise ValueError(
                    f"Different entries claim the same publication key: {claim}"
                )
            claims[claim] = entry.id
            bindings.append(
                {**raw, "before": translation_at(document(file), binding.path)}
            )
        bindings = list({(b["file"], tuple(b["path"])): b for b in bindings}.values())
        valid = lambda b: bool(b["before"] and b["before"].strip())
        reuse = None
        if entry.reconcile:
            if not check_existing and all(valid(b) for b in bindings):
                continue
            populated = [b for b in bindings if valid(b)]
            if populated:
                priority = max(b["priority"] for b in populated)
                values = {b["before"] for b in populated if b["priority"] == priority}
                if len({b["before"] for b in populated}) > 1:
                    conflicts.append(
                        {
                            "entry_id": entry.id,
                            "source": entry.source,
                            "outputs": [
                                {
                                    "file": b["file"],
                                    "path": b["path"],
                                    "translation": b["before"],
                                    "priority": b["priority"],
                                }
                                for b in populated
                            ],
                            "missing_outputs": sum(not valid(b) for b in bindings),
                            "blocked": len(values) > 1
                            and any(not valid(b) for b in bindings),
                        }
                    )
                if len(values) != 1:
                    blocked_files.update(
                        b["file"]
                        for b in bindings
                        if b["file"] in atomic_files and not valid(b)
                    )
                    continue
                reuse = next(iter(values))
            bindings = [b for b in bindings if not valid(b)]
            if not bindings:
                continue
            reason = "linked_outputs"
        else:
            bindings = [b for b in bindings if not valid(b)]
            if not bindings:
                continue
            reason = "missing"
            if entry.use_terms:
                term = term_for(glossary, entry.source, entry.category)
                reuse = term.translation if term else None
        rules = combine_rules(entry.rules, target.rules)
        revision = digest(
            [
                project.source_language,
                entry.category,
                entry.term,
                entry.use_terms,
                entry.source,
                entry.context_version,
                entry.context,
                rules,
                target.style,
            ]
        )
        tasks.append(
            {
                "id": digest([project.id, entry.id, target.code, revision]),
                "entry_id": entry.id,
                "revision": revision,
                "category": entry.category,
                "group": entry.group,
                "source": entry.source,
                "context": entry.context,
                "context_version": entry.context_version,
                "references": entry.references,
                "targets": bindings,
                "term": entry.term,
                "use_terms": entry.use_terms,
                "rules": rules,
                "reuse": reuse,
                "reason": reason,
            }
        )
    tasks.sort(key=lambda task: not task["term"])
    available = len(tasks)
    tasks = select_tasks(tasks, limit, atomic_files, blocked_files)
    plan = Plan.model_validate(
        {
            "version": 7,
            "id": "pending",
            **identity,
            "project_name": project.name,
            "language_name": target.name,
            "adapter": project.adapter,
            "adapter_options": options,
            "rules": target.rules,
            "style": target.style,
            "source_version": catalog.source_version,
            "source_files": catalog.source_files,
            "check_existing": check_existing,
            "term_bindings": term_bindings,
            "published_files": published_files(target.translations),
            "tasks": tasks,
        }
    )
    plan.id = digest(plan.model_dump(exclude={"id"}))
    write_json(target.work / "plan.json", plan.model_dump())
    bind_runtime(target.work, project, target)
    report = {
        "plan": plan.id,
        **identity,
        "tasks": len(tasks),
        "source_files": catalog.source_files,
        "blocked_files": sorted(blocked_files),
        "existing_variants": conflicts,
        "blocked_entries": sum(item["blocked"] for item in conflicts),
        "source_scope": "complete" if catalog.complete else "partial",
        "unmapped_outputs": [
            {"file": file, "path": list(keys)}
            for file in plan.published_files
            if check_existing and catalog.complete
            for keys in output_paths(document(file))
            if (file, keys) not in claims
        ],
        "available_tasks": available,
        "reuse_candidates": sum(t["reuse"] is not None for t in tasks),
        "by_category": {
            category: sum(task.category == category for task in plan.tasks)
            for category in sorted({task.category for task in plan.tasks})
        },
    }
    write_json(target.work / "prepare-report.json", report)
    print(
        f"{target.code}: selected {len(tasks)}/{available} tasks; {report['reuse_candidates']} reuse candidates; "
        f"{report['blocked_entries']} entries blocked by ambiguous translations. Report: {target.work / 'prepare-report.json'}",
        flush=True,
    )
    return plan
