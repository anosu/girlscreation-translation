"""Plan one target from a shared source catalog and its current publication."""

import hashlib
import os
from pathlib import Path

from scripts.adapters import load_adapter
from scripts.config import Project, Target
from scripts.glossary import (
    observed_policy,
    project_terms,
    read_optional,
    resolve_glossary,
    review_outputs,
    term_for,
)
from scripts.models import Catalog, CompiledCatalog, Plan, TermBinding, read_state
from scripts.utils import digest, directory_digest, read_json, write_json
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
                "state": target.state,
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
    adapter = load_adapter(project.adapter)
    files = {"adapter": Path(getattr(adapter, "__file__"))}
    for key, value in adapter.settings(project.options).items():
        if isinstance(value, str) and (project.root / value).is_file():
            files[key] = project.root / value
    return digest(
        {
            "adapter": project.adapter,
            "options": project.options,
            "files": {
                key: hashlib.sha256(path.read_bytes()).hexdigest()
                for key, path in files.items()
            },
        }
    )


def source_catalog(project: Project, compiled: Path | None = None) -> Catalog:
    if (project.sources / ".fetching").exists():
        raise ValueError(
            "Source fetch was interrupted; rerun fetch before preparing tasks"
        )
    adapter = load_adapter(project.adapter)
    version = directory_digest(project.sources)
    if compiled is not None:
        artifact = CompiledCatalog.model_validate(read_json(compiled))
        if (
            artifact.project != project.id
            or artifact.inputs != catalog_inputs(project)
            or artifact.catalog.source_version != version
        ):
            raise ValueError("Shared catalog is stale; run catalog again")
        return artifact.catalog
    options = {**adapter.settings(project.options), "root": str(project.root)}
    catalog = adapter.extract(project.sources, options)
    catalog.source_version = version
    return catalog


def compile_catalog(project: Project) -> Catalog:
    catalog = source_catalog(project)
    write_json(
        project.catalog,
        CompiledCatalog(
            project=project.id, inputs=catalog_inputs(project), catalog=catalog
        ).model_dump(),
    )
    return catalog


def prepare_tasks(
    project: Project,
    target: Target,
    catalog: Catalog | None = None,
    limit: int | None = None,
) -> Plan:
    """Preserve published text, select missing/revised entries, and plan terms first."""
    if limit is not None and limit <= 0:
        raise ValueError("Task limit must be greater than zero")
    old_plan = target.work / "plan.json"
    if old_plan.exists() and read_json(old_plan).get("version") != 5:
        raise ValueError(
            "This work directory contains legacy artifacts; configure a new work directory. The old cache has been preserved."
        )
    adapter = load_adapter(project.adapter)
    options = adapter.settings(project.options)
    catalog = adapter.publication(
        catalog or source_catalog(project), target.translations, options
    )
    state = read_state(target.state)
    identity = {
        "project": project.id,
        "language": target.code,
        "source_language": project.source_language,
    }
    if state and any(state.get(k) != v for k, v in identity.items()):
        raise ValueError(
            "Source state belongs to another project, source language or target; migrate explicitly"
        )
    term_bindings = list(
        {
            digest(b.model_dump()): b
            for b in [
                *[
                    TermBinding.model_validate(value)
                    for value in state.get("term_bindings", [])
                ],
                *catalog.term_bindings,
            ]
        }.values()
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
    for entry in catalog.entries:
        bindings = []
        source_revision = digest([project.source_language, entry.source])
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
        valid = lambda b: (
            bool(b["before"] and b["before"].strip())
            and (
                not b["track_source"]
                or state.get("sources", {}).get(entry.id) == source_revision
            )
        )
        reuse = None
        if entry.reconcile:
            populated = [b for b in bindings if valid(b)]
            if populated:
                priority = max(b["priority"] for b in populated)
                values = {b["before"] for b in populated if b["priority"] == priority}
                if len(values) != 1:
                    raise ValueError(
                        f"Conflicting equal-priority outputs for {entry.id}: {values}"
                    )
                reuse = next(iter(values))
            if reuse is not None and all(
                valid(b) and b["before"] == reuse for b in bindings
            ):
                continue
            reason = "linked_outputs"
        else:
            bindings = [b for b in bindings if not valid(b)]
            if not bindings:
                continue
            reason = (
                "source_changed" if any(b["before"] for b in bindings) else "missing"
            )
            if entry.use_terms and reason == "missing":
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
    if limit is not None:
        tasks = tasks[:limit]
    plan = Plan.model_validate(
        {
            "version": 5,
            "id": "pending",
            **identity,
            "project_name": project.name,
            "language_name": target.name,
            "adapter": project.adapter,
            "adapter_options": options,
            "rules": target.rules,
            "style": target.style,
            "source_version": catalog.source_version,
            "source_state_before": digest(state),
            "term_bindings": term_bindings,
            "published_files": sorted(
                p.relative_to(target.translations).as_posix()
                for p in target.translations.rglob("*.json")
                if p.name != "manifest.json"
            ),
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
        "source_scope": "complete" if catalog.complete else "partial",
        "unmapped_outputs": [
            {"file": file, "path": list(keys)}
            for file in plan.published_files
            if catalog.complete
            for keys in output_paths(document(file))
            if (file, keys) not in claims
        ],
        "available_tasks": available,
        "reuse_candidates": sum(t["reuse"] is not None for t in tasks),
        "by_category": {
            category: sum(task.category == category for task in plan.tasks)
            for category in sorted({task.category for task in plan.tasks})
        },
        "review_outputs": review_outputs(
            state,
            observed_policy(
                target.style, target.rules, read_optional(target.glossary), names, terms
            ),
            plan.published_files,
        ),
    }
    write_json(target.work / "prepare-report.json", report)
    print(
        f"{target.code}: selected {len(tasks)}/{available} tasks; {report['reuse_candidates']} reuse candidates; "
        f"{len(report['review_outputs'])} files to review. Report: {target.work / 'prepare-report.json'}",
        flush=True,
    )
    return plan
