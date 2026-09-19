"""Canonical terms, explicit category overrides and publication projections."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import Field, model_validator

from workflow.config import StrictModel, TermSource, Text
from workflow.dictionaries import dictionary_at, read_document
from workflow.models import (
    ResolvedTerm,
    Task,
    TermIndex,
    TermProposal,
)
from workflow.utils import read_json
from workflow.validate import validate_translation


class GlossaryEntry(StrictModel):
    translation: Text | None = None
    note: str = ""
    categories: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def exclusive_value(self):
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


def project_terms(
    translations: Path,
    sources: Sequence[TermSource],
    tasks: Mapping[str, Task] | None = None,
    values: Mapping[str, str] | None = None,
    documents: Mapping[str, dict] | None = None,
) -> dict[str, str]:
    """Read each document once; project answers without copying or mutating documents."""
    data = dict(documents or {})

    for file in {source.file for source in sources}:
        path = (translations / file).resolve()
        if not path.is_relative_to(translations.resolve()):
            raise ValueError(f"Term source escapes translations: {file}")
        if file not in data:
            data[file] = read_document(path)

    overlay: dict[tuple[str, tuple[str, ...]], dict[str, str]] = {}
    for key, value in (values or {}).items():
        task = (tasks or {})[key]
        overlay.setdefault((task.output, tuple(task.path)), {})[task.source] = value
    names: dict[str, str] = {}
    for source in sources:
        values_at_path = dict(dictionary_at(data[source.file], source.path))
        values_at_path.update(overlay.get((source.file, tuple(source.path)), {}))
        for original, translated in values_at_path.items():
            if not translated.strip():
                continue
            if original in names and names[original] != translated:
                raise ValueError(f"Conflicting canonical term: {original}")
            names[original] = translated
    return names


def resolve_glossary(path: Path | dict, names: dict[str, str]) -> TermIndex:
    """Automatic names establish globals; explicit scopes override only their categories."""
    base = names
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
                entry.translation if entry.translation is not None else base.get(source)
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
    rules: dict | None = None,
) -> dict:
    """Add evidenced scopes without replacing an existing definition in that scope."""
    result = dict(existing)
    known = resolve_glossary(result, names)
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
        if source not in task.source:
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
        if not proposal.categories and names.get(source) == target:
            # The published canonical dictionary already owns this translation.
            continue
        entry = GlossaryEntry(
            translation=target,
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
        known = resolve_glossary(result, names)
    for key, translated in values.items():
        task = tasks[key]
        term = term_for(known, task.source, task.category)
        if term and translated != term.translation:
            raise ValueError(
                f"Term disagrees with an accepted translation: {task.source}"
            )
    return result
