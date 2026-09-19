"""Offline planning into exact source keys within resource-level dictionary paths."""

import os
from pathlib import Path

from workflow.config import Project, Target, TermSource
from workflow.dictionaries import (
    dictionary_at,
    read_document,
    validate_locations,
)
from workflow.glossary import project_terms, resolve_glossary, term_for
from workflow.models import PLAN_VERSION, Plan
from workflow.snapshot import read_resource, read_snapshot
from workflow.utils import digest, read_json, write_json
from workflow.validate import combine_rules


def bind_runtime(work: Path, project: Project, target: Target) -> None:
    def location(path):
        try:
            return Path(os.path.relpath(path, work)).as_posix()
        except ValueError:
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
    data = read_json(work / "plan.json")
    if data.get("version") != PLAN_VERSION:
        raise ValueError(
            "This cached plan is from an older workflow. Run plan, then translate; keep the existing work directory and dictionaries."
        )
    plan = Plan.model_validate(data)
    if plan.id != digest(plan.model_dump(exclude={"id"})):
        raise ValueError("Plan contents changed; run plan again")
    return plan


def prepare_tasks(project: Project, target: Target, limit: int | None = None) -> Plan:
    """Select whole output dictionaries, so limits cannot strand partial resources."""
    if limit is not None and limit <= 0:
        raise ValueError("Resource limit must be positive")
    snapshot, files, version = read_snapshot(project)
    resources = [
        read_resource(project.sources, file) for file in snapshot.resources.values()
    ]
    canonical = {(source.file, tuple(source.path)) for source in target.term_sources}

    def is_term(resource):
        return resource.term or (resource.output, tuple(resource.path)) in canonical

    resources.sort(key=lambda resource: not is_term(resource))
    validate_locations(
        [
            *((resource.output, resource.path) for resource in resources),
            *((source.file, source.path) for source in target.term_sources),
        ]
    )
    by_output = {}
    documents = {}
    pending_resources = set()
    pending_keys = set()
    for resource in resources:
        by_output.setdefault(resource.output, []).append(resource)
        output = (target.translations / resource.output).resolve()
        if not output.is_relative_to(target.translations.resolve()):
            raise ValueError("Publication path escapes target")
        if resource.output not in documents:
            documents[resource.output] = read_document(output)
        dictionary = dictionary_at(documents[resource.output], resource.path)
        for occurrence in resource.occurrences():
            source = occurrence["source"]
            before = dictionary.get(source)
            if not before or not before.strip():
                pending_resources.add(resource.id)
                pending_keys.add((resource.output, tuple(resource.path), source))
    # Completed resources remain readable context but do not consume the limit.
    # Every selected output still includes all its resources, including shared keys.
    selected = []
    selected_pending = 0
    required_sizes = []
    for group in by_output.values():
        cost = sum(resource.id in pending_resources for resource in group)
        if cost:
            required_sizes.append(cost)
        if limit is None or selected_pending + cost <= limit:
            selected.extend(group)
            selected_pending += cost
    if pending_resources and not selected_pending:
        raise ValueError(
            "Resource limit cannot fit a complete output dictionary; "
            f"use at least {min(required_sizes)} pending resources or omit --limit"
        )
    claims = {}
    term_dictionaries = {
        (resource.output, tuple(resource.path)): TermSource(
            file=resource.output, path=resource.path
        )
        for resource in selected
        if resource.term
    }
    dictionaries = {
        **{(source.file, tuple(source.path)): source for source in target.term_sources},
        **term_dictionaries,
    }
    for resource in selected:
        dictionary = dictionary_at(documents[resource.output], resource.path)
        reference = snapshot.resources[resource.id]
        for occurrence in resource.occurrences():
            source = occurrence["source"]
            key = (resource.output, tuple(resource.path), source)
            rules = combine_rules(resource.rules, target.rules)
            if key not in claims:
                claims[key] = {
                    "source": source,
                    "category": "terms" if is_term(resource) else resource.kind,
                    "group": resource.id,
                    "references": [],
                    "rules": rules,
                    "term": is_term(resource),
                    "output": resource.output,
                    "path": resource.path,
                    "before": dictionary.get(source),
                }
            claim = claims[key]
            # Contradictory exact-term policies cannot be silently overwritten.
            old = claim["rules"].get("required_terms", {})
            new = rules.get("required_terms", {})
            if any(k in old and old[k] != value for k, value in new.items()):
                raise ValueError(f"Incompatible rules for repeated source: {source}")
            claim["rules"] = combine_rules(claim["rules"], rules)
            claim["rules"]["required_terms"] = {**old, **new}
            if reference not in claim["references"]:
                claim["references"].append(reference)
            claim["term"] |= is_term(resource)
    names = project_terms(
        target.translations,
        list(dictionaries.values()),
        documents=documents,
    )
    glossary = resolve_glossary(target.glossary, names)
    tasks = []
    for (output, path, source), task in claims.items():
        before = task["before"]
        if before and before.strip():
            continue
        task["id"] = digest(
            [
                project.id,
                project.source_language,
                target.code,
                output,
                path,
                source,
                task["term"],
                task["category"],
                task["references"],
                task["rules"],
                target.style,
            ]
        )
        term = term_for(glossary, source, task["category"])
        task["reuse"] = term.translation if term else None
        tasks.append(task)
    tasks.sort(key=lambda task: not task["term"])
    selected_files = {r.id: snapshot.resources[r.id] for r in selected}
    plan = Plan.model_validate(
        {
            "version": PLAN_VERSION,
            "id": "pending",
            "project": project.id,
            "project_name": project.name,
            "source_language": project.source_language,
            "language": target.code,
            "language_name": target.name,
            "rules": target.rules,
            "style": target.style,
            "source_version": version,
            "source_files": files,
            "resources": selected_files,
            "term_dictionaries": [
                source.model_dump() for source in term_dictionaries.values()
            ],
            "term_sources": [source.model_dump() for source in target.term_sources],
            "tasks": tasks,
        }
    )
    plan.id = digest(plan.model_dump(exclude={"id"}))
    write_json(target.work / "plan.json", plan.model_dump())
    bind_runtime(target.work, project, target)
    write_json(
        target.work / "prepare-report.json",
        {
            "plan": plan.id,
            "tasks": len(tasks),
            "resources": len(selected),
            "available_resources": len(resources),
            "pending_resources": selected_pending,
            "available_pending_resources": len(pending_resources),
            "available_tasks": len(pending_keys),
            "deferred_tasks": len(pending_keys) - len(tasks),
            "reuse_candidates": sum(t["reuse"] is not None for t in tasks),
            "source_scope": "selected resources only",
            "blocked_entries": 0,
        },
    )
    print(
        f"{target.code}: {len(selected)}/{len(resources)} resources "
        f"({selected_pending}/{len(pending_resources)} pending), "
        f"{len(tasks)}/{len(pending_keys)} missing dictionary keys selected"
    )
    return plan
