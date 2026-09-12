"""Preflight complete publications before applying any selected target's update."""

import json
from dataclasses import dataclass
from pathlib import Path

from scripts.build import make_manifest, traverse
from scripts.config import Project, Target
from scripts.glossary import (
    apply_proposals,
    observed_policy,
    project_terms,
    read_optional,
    resolve_glossary,
    review_outputs,
    term_for,
)
from scripts.models import Results, read_state
from scripts.prepare import read_plan, translation_at
from scripts.utils import digest, read_json, write_bytes, write_json
from scripts.validate import validate_results


def encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


@dataclass
class Update:
    root: Path
    originals: dict[Path, bytes | None]
    files: dict[Path, bytes]
    existing_files: set[Path]
    work: Path
    plan_id: str

    def check(self) -> None:
        if set(self.root.rglob("*.json")) != self.existing_files:
            raise ValueError("Publication files changed during preflight")
        for path, before in self.originals.items():
            if (path.read_bytes() if path.exists() else None) != before:
                raise ValueError(f"Concurrent publication edit detected: {path}")


def prepare_update(project: Project, target: Target) -> Update:
    plan = read_plan(target.work)
    if (
        plan.project != project.id
        or plan.language != target.code
        or plan.source_language != project.source_language
    ):
        raise ValueError("Plan belongs to another project, source language or target")
    if plan.style != target.style or plan.rules != target.rules:
        raise ValueError("Target translation policy changed since prepare")
    result = Results.model_validate(read_json(target.work / "results.json"))
    if (
        result.plan != plan.id
        or result.project != project.id
        or result.language != target.code
    ):
        raise ValueError("Results belong to another plan, project or target")
    values = validate_results(
        plan.tasks, {"translations": result.translations}, plan.rules
    )
    existing = set(target.translations.rglob("*.json"))
    published = {path: path.read_bytes() for path in existing}
    originals: dict[Path, bytes | None] = dict(published)
    documents, claims = {}, {}
    for task in plan.tasks:
        for binding in task.targets:
            file = binding.file
            path = target.translations / file
            if not path.resolve().is_relative_to(target.translations.resolve()):
                raise ValueError(f"Publication target escapes translation root: {file}")
            claim = (file, tuple(binding.path))
            if claim in claims and claims[claim] != task.id:
                raise ValueError(f"Two tasks claim {claim}")
            claims[claim] = task.id
            if file not in documents:
                documents[file] = read_optional(path)
                originals.setdefault(path, None)
            current = translation_at(documents[file], binding.path)
            if current not in (binding.before, values[task.id]):
                raise ValueError(
                    f"Translation changed since prepare: {file}: {binding.path}"
                )
            parent = documents[file]
            for part in binding.path[:-1]:
                parent = parent.setdefault(part, {})
            parent[binding.path[-1]] = values[task.id]
    glossary_before = read_optional(target.glossary)
    if digest(glossary_before) not in (
        result.glossary_before,
        result.glossary_after,
    ):
        raise ValueError("Glossary changed since agent setup")
    names, terms = project_terms(
        target.translations, plan.term_bindings, documents=documents
    )
    glossary_after = apply_proposals(
        result.glossary_proposals,
        {t.id: t for t in plan.tasks},
        values,
        glossary_before,
        names,
        terms,
        plan.rules,
    )
    resolved = resolve_glossary(glossary_after, names, terms)
    for task in plan.tasks:
        canonical = term_for(resolved, task.source, task.category)
        if (
            (task.term or task.use_terms)
            and canonical is not None
            and values[task.id] != canonical.translation
        ):
            raise ValueError(
                f"Translation conflicts with established term: {task.source}"
            )
    # Every destination was checked against its recorded before/after value above.
    # Validate the fully projected result, allowing any safely applied subset after interruption.
    if digest([names, terms]) != result.published_terms_after:
        raise ValueError("Published terminology changed since setup")
    state_before = read_state(target.state)
    bindings = {
        t.entry_id: digest([project.source_language, t.source])
        for t in plan.tasks
        if any(b.track_source for b in t.targets)
    }
    identity = {
        "project": project.id,
        "language": target.code,
        "source_language": project.source_language,
    }
    if state_before and any(state_before.get(k) != v for k, v in identity.items()):
        raise ValueError("Source state belongs to another project or language")
    if bindings:
        if digest(state_before) != plan.source_state_before and any(
            state_before.get("sources", {}).get(k) != v for k, v in bindings.items()
        ):
            raise ValueError("Source state changed since prepare")
    policy = observed_policy(target.style, target.rules, glossary_after, names, terms)
    state_after = {
        **identity,
        "sources": {**state_before.get("sources", {}), **bindings},
        "term_bindings": [binding.model_dump() for binding in plan.term_bindings],
        "observed_policy": policy,
        "review_outputs": review_outputs(
            state_before,
            policy,
            plan.published_files,
        ),
    }
    files = {
        target.translations / name: encoded(data) for name, data in documents.items()
    }
    for path, value, before in [
        (target.glossary, glossary_after, glossary_before),
        (target.state, state_after, state_before),
    ]:
        originals[path] = path.read_bytes() if path.exists() else None
        if value != before:
            files[path] = encoded(value)
    content = {
        path.relative_to(target.translations).as_posix(): raw
        for path, raw in published.items()
    }
    content.update({name: encoded(data) for name, data in documents.items()})
    for name, raw in content.items():
        if name == "manifest.json":
            continue
        for key, value in traverse(json.loads(raw)):
            if not value.strip() or any(
                term in value for term in target.rules.get("forbidden_translations", [])
            ):
                raise ValueError(f"Invalid published translation: {name}: {key}")
    manifest = target.translations / "manifest.json"
    originals.setdefault(manifest, None)
    files[manifest] = make_manifest(content)
    return Update(
        target.translations,
        originals,
        {p: raw for p, raw in files.items() if raw != originals[p]},
        existing,
        target.work,
        plan.id,
    )


def apply_updates(updates: list[Update]) -> int:
    """Check every target first. File replacements are atomic; the batch is restartable."""
    for update in updates:
        update.check()
    changed = 0
    for update in updates:
        for path, content in sorted(
            update.files.items(), key=lambda item: item[0].name == "manifest.json"
        ):
            write_bytes(path, content)
            changed += 1
    for update in updates:
        write_json(update.work / "publication.json", {"plan": update.plan_id})
    return changed


def merge_results(project: Project, target: Target) -> int:
    return apply_updates([prepare_update(project, target)])
