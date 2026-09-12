"""Data crossing the game, planning and agent interfaces."""

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from scripts.config import Rules, StrictModel, Text
from scripts.utils import read_json


class Binding(StrictModel):
    file: str
    path: list[str]
    priority: int = 0
    track_source: bool = True

    @model_validator(mode="after")
    def valid_path(self):
        path = PurePosixPath(self.file)
        if (
            not self.file
            or "\\" in self.file
            or ":" in self.file
            or path.is_absolute()
            or ".." in path.parts
            or any(p.startswith(".") for p in path.parts)
            or not self.file.endswith(".json")
            or path.name == "manifest.json"
            or not self.path
            or not all(self.path)
        ):
            raise ValueError(f"Invalid translation target: {self.file}: {self.path}")
        return self


class Entry(StrictModel):
    id: Text
    category: Text
    group: Text
    source: str = Field(min_length=1)
    context: dict | list
    context_version: Text
    targets: list[Binding] = Field(min_length=1)
    references: list[Text] = Field(default_factory=list)
    term: bool = False
    use_terms: bool = False
    reconcile: bool = False
    rules: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_source(self):
        if not self.source.strip():
            raise ValueError("Source text must not be blank")
        Rules.model_validate(self.rules)
        return self


class TermBinding(StrictModel):
    source: str = Field(min_length=1)
    target: Binding
    reference: Text | None = None


class Catalog(StrictModel):
    source_version: str = ""
    complete: bool = True
    entries: list[Entry]
    term_bindings: list[TermBinding] = Field(default_factory=list)
    source_files: list[str] | None = None
    target_files: dict[str, Text] = Field(default_factory=dict)
    atomic_files: list[str] = Field(default_factory=list)
    check_existing: bool = False

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [entry.id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("Adapter emitted duplicate entry IDs")
        return self


class CompiledCatalog(StrictModel):
    project: Text
    inputs: Text
    catalog: Catalog


class PlannedBinding(Binding):
    before: str | None


class Task(StrictModel):
    id: Text
    entry_id: Text
    revision: Text
    category: Text
    group: Text
    source: str = Field(min_length=1)
    context: dict | list
    context_version: Text
    references: list[Text]
    targets: list[PlannedBinding] = Field(min_length=1)
    term: bool
    use_terms: bool
    rules: dict
    reuse: str | None
    reason: Literal["missing", "source_changed", "linked_outputs"]


class Plan(StrictModel):
    version: Literal[6]
    id: Text
    project: Text
    project_name: Text
    source_language: Text
    language: Text
    language_name: Text
    adapter: Text
    adapter_options: dict
    rules: dict
    style: str
    source_state_before: Text
    source_version: Text
    source_files: list[str] | None = None
    check_existing: bool = False
    term_bindings: list[TermBinding]
    published_files: list[str] = Field(default_factory=list)
    tasks: list[Task]


class Translation(StrictModel):
    id: Text
    translation: str


class Submission(StrictModel):
    translations: list[Translation]


class Answer(Translation):
    policy: Text


class TermProposal(StrictModel):
    source: Text
    translation: Text
    note: Text
    evidence: Text
    categories: list[Text] = Field(default_factory=list)


class ResolvedTerm(StrictModel):
    translation: Text
    note: str = ""
    categories: list[Text] = Field(default_factory=list)

    @field_validator("categories")
    @classmethod
    def ordered_categories(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


type TermIndex = dict[str, list[ResolvedTerm]]


class Results(Submission):
    version: Literal[6]
    plan: Text
    project: Text
    language: Text
    glossary_before: Text
    glossary_after: Text
    published_terms_after: Text
    glossary_proposals: list[TermProposal]

    @model_validator(mode="before")
    @classmethod
    def discard_unused_fingerprint(cls, value):
        """Discard the retired fingerprint; merge validates the projected terminology."""
        if isinstance(value, dict) and "published_terms_before" in value:
            return {
                key: item
                for key, item in value.items()
                if key != "published_terms_before"
            }
        return value


class ObservedPolicy(StrictModel):
    style: Text
    rules: Text
    terms: TermIndex

    @field_validator("terms", mode="before")
    @classmethod
    def legacy_terms(cls, values: dict) -> dict:
        result = {}
        for source, entries in values.items():
            if isinstance(entries, dict):
                entry = dict(entries)
                if "categories" not in entry and (
                    entry.get("note", "").startswith("Existing terminology: ")
                    or entry.get("note") == "Established term reference"
                ):
                    entry.pop("note", None)
                entries = [entry]
            result[source] = entries
        return result


class SourceState(StrictModel):
    project: Text
    language: Text
    source_language: Text
    sources: dict[str, Text] = Field(default_factory=dict)
    term_bindings: list[TermBinding] = Field(default_factory=list)
    observed_policy: ObservedPolicy | None = None
    review_outputs: list[Text] = Field(default_factory=list)


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    data = read_json(path)
    return {} if data == {} else SourceState.model_validate(data).model_dump()
