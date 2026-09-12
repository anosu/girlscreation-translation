"""Canonical terms, explicit category overrides and publication projections."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import Field, model_validator

from scripts.config import StrictModel, Text
from scripts.models import (
    ObservedPolicy,
    ResolvedTerm,
    Task,
    TermBinding,
    TermIndex,
    TermProposal,
)
from scripts.utils import digest, read_json
from scripts.validate import validate_translation


class GlossaryEntry(StrictModel):
    reference: Text | None = None
    translation: Text | None = None
    note: str = ""
    categories: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def exclusive_value(self):
        if (self.reference is None) == (self.translation is None):
            raise ValueError("Provide exactly one reference or translation")
        self.categories = sorted(set(self.categories))
        return self


def read_optional(path: Path) -> dict:
    return read_json(path) if path.exists() else {}


def term_for(terms: TermIndex, source: str, category: str) -> ResolvedTerm | None:
    entries = terms.get(source, [])
    return next(
        (term for term in entries if category in term.categories),
        next((term for term in entries if not term.categories), None),
    )


def terms_payload(terms: TermIndex) -> dict:
    """Only semantic information enters prompts and cache fingerprints."""
    return {
        source: [term.model_dump() for term in entries]
        for source, entries in sorted(terms.items())
    }


def observed_policy(
    style: str, rules: dict, glossary: dict, names: dict, tables: dict
) -> dict:
    return {
        "style": digest(style),
        "rules": digest(rules),
        "terms": terms_payload(resolve_glossary(glossary, names, tables)),
    }


def review_outputs(state: dict, current: dict, files: list[str]) -> list[str]:
    previous = state.get("observed_policy")
    if previous:
        previous = ObservedPolicy.model_validate(previous).model_dump()
    changed = previous and (
        any(previous[key] != current[key] for key in ("style", "rules"))
        or any(
            current["terms"].get(source) != value
            for source, value in previous["terms"].items()
        )
    )
    return sorted(
        set(state.get("review_outputs", [])) | (set(files) if changed else set())
    )


def project_terms(
    translations: Path,
    bindings: Sequence[TermBinding],
    tasks: Mapping[str, Task] | None = None,
    values: Mapping[str, str] | None = None,
    documents: Mapping[str, dict] | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Read each document once; project answers without copying or mutating documents."""
    data = dict(documents or {})
    for binding in bindings:
        file = binding.target.file
        if file not in data:
            data[file] = read_optional(translations / file)
    overlay = {
        (target.file, tuple(target.path)): value
        for key, value in (values or {}).items()
        for target in (tasks or {})[key].targets
    }
    names: dict[str, str] = {}
    tables: dict[str, dict[str, str]] = {}
    for binding in bindings:
        location = binding.target
        key = (location.file, tuple(location.path))
        value: object = overlay.get(key)
        if key not in overlay:
            value = data[location.file]
            for part in location.path:
                value = value.get(part) if isinstance(value, dict) else None
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"Term binding must resolve to text: {location}")
        if binding.reference:
            if binding.reference in names and names[binding.reference] != value:
                raise ValueError(f"Conflicting term reference: {binding.reference}")
            names[binding.reference] = value
        table = tables.setdefault(location.file, {})
        if binding.source in table and table[binding.source] != value:
            raise ValueError(
                f"Conflicting term outputs: {binding.source}: {location.file}"
            )
        table[binding.source] = value
    return names, tables


def resolve_glossary(
    path: Path | dict, names: dict[str, str], master: dict | None = None
) -> TermIndex:
    """Automatic names establish globals; explicit scopes override only their categories."""
    base = dict(names)
    for table, entries in (master or {}).items():
        for source, target in entries.items():
            if not isinstance(target, str):
                raise ValueError(f"Term source must contain text: {table}: {source}")
            if not target.strip():
                continue
            if source in base and base[source] != target:
                raise ValueError(f"Master/name conflict: {table}: {source}")
            base[source] = target
    resolved: TermIndex = {
        source: [ResolvedTerm(translation=target)] for source, target in base.items()
    }
    for source, raw in (
        read_optional(path) if isinstance(path, Path) else path
    ).items():
        globals_ = resolved.get(source, [])
        explicit: list[ResolvedTerm] = []
        claimed: set[str] = set()
        for value in raw if isinstance(raw, list) else [raw]:
            entry = GlossaryEntry.model_validate(value)
            target = (
                names.get(entry.reference) if entry.reference else entry.translation
            )
            if not target:
                raise ValueError(f"Unresolved glossary entry: {source}")
            scopes = set(entry.categories or [""])
            if claimed & scopes:
                raise ValueError(
                    f"Overlapping glossary scopes: {source}: {sorted(claimed & scopes)}"
                )
            claimed.update(scopes)
            if not entry.categories:
                if source in base and base[source] != target:
                    raise ValueError(
                        f"Glossary/name conflict: {source}: {target!r} != {base[source]!r}"
                    )
                globals_ = []
            explicit.append(
                ResolvedTerm(
                    translation=target, note=entry.note, categories=entry.categories
                )
            )
        if not explicit:
            raise ValueError(f"Empty glossary entry: {source}")
        resolved[source] = sorted(
            [*globals_, *explicit], key=lambda term: term.categories
        )
    return resolved


def apply_proposals(
    proposals: Sequence[TermProposal],
    tasks: Mapping[str, Task],
    values: Mapping[str, str],
    existing: dict,
    names: dict[str, str],
    master: dict,
    rules: dict | None = None,
) -> dict:
    """Add evidenced scopes without replacing an existing definition in that scope."""
    result = dict(existing)
    known = resolve_glossary(result, names, master)
    seen: set[tuple[str, str]] = set()
    for proposal in proposals:
        source, target = proposal.source, proposal.translation
        scopes = set(proposal.categories or [""])
        keys = {(source, scope) for scope in scopes}
        if keys & seen:
            raise ValueError(f"Duplicate proposed term scope: {source}")
        seen.update(keys)
        task = tasks.get(proposal.evidence)
        if task is None:
            raise ValueError(f"Unknown evidence task for {source}")
        if proposal.categories and task.category not in proposal.categories:
            raise ValueError(f"Evidence category is outside proposed scopes: {source}")
        if source not in task.source and source not in json.dumps(
            task.context, ensure_ascii=False
        ):
            raise ValueError(f"Term {source} does not occur in its evidence")
        validate_translation(source, target, rules)
        previous_entries = result.get(source, [])
        previous_entries = (
            previous_entries
            if isinstance(previous_entries, list)
            else [previous_entries]
        )
        occupied = {
            scope
            for raw in previous_entries
            for scope in (GlossaryEntry.model_validate(raw).categories or [""])
        } & scopes
        for term in known.get(source, []):
            overlap = scopes & set(term.categories or [""])
            if overlap and term.translation != target:
                raise ValueError(
                    f"Existing term conflict: {source}: {term.translation}"
                )
        remaining = scopes - occupied
        if not remaining:
            continue
        entry = GlossaryEntry(
            reference=source
            if not proposal.categories and names.get(source) == target
            else None,
            translation=None
            if not proposal.categories and names.get(source) == target
            else target,
            note=proposal.note,
            categories=sorted(remaining - {""}),
        ).model_dump(exclude_none=True)
        previous = result.get(source)
        entries = (
            []
            if previous is None
            else previous
            if isinstance(previous, list)
            else [previous]
        ) + [entry]
        result[source] = entries[0] if len(entries) == 1 else entries
        known = resolve_glossary(result, names, master)
    for key, translated in values.items():
        task = tasks[key]
        term = term_for(known, task.source, task.category)
        if term and (task.term or task.use_terms) and translated != term.translation:
            raise ValueError(
                f"Term disagrees with an accepted translation: {task.source}"
            )
    return result
