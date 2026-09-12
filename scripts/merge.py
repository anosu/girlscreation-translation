"""Preflight complete publications before applying any selected target's update."""

import json
from dataclasses import dataclass
from pathlib import Path

from scripts.build import make_manifest, traverse
from scripts.config import Project, Target
from scripts.glossary import (
    apply_proposals,
    project_terms,
    read_optional,
    resolve_glossary,
    term_for,
)
from scripts.models import Results
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
    inspected = (
        existing
        if plan.tasks or plan.check_existing
        else {target.translations / b.target.file for b in plan.term_bindings}
        & existing
    )
    published = {path: path.read_bytes() for path in inspected}
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
    files = {
        target.translations / name: encoded(data) for name, data in documents.items()
    }
    originals[target.glossary] = (
        target.glossary.read_bytes() if target.glossary.exists() else None
    )
    if glossary_after != glossary_before:
        files[target.glossary] = encoded(glossary_after)
    content = {
        path.relative_to(target.translations).as_posix(): raw
        for path, raw in published.items()
    }
    content.update({name: encoded(data) for name, data in documents.items()})
    checked = (
        content
        if plan.check_existing
        else {name: encoded(data) for name, data in documents.items()}
    )
    for name, raw in checked.items():
        if name == "manifest.json":
            continue
        for key, value in traverse(json.loads(raw)):
            if not value.strip() or any(
                term in value for term in target.rules.get("forbidden_translations", [])
            ):
                raise ValueError(f"Invalid published translation: {name}: {key}")
    if documents or plan.check_existing:
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
