"""Private planning and publication records; adapter input lives in resources.py."""

from pydantic import Field, field_validator, model_validator

from workflow.config import Rules, StrictModel, TermSource, Text
from workflow.dictionaries import namespace_path, output_path, validate_locations
from workflow.version import PlanVersion


class Task(StrictModel):
    id: Text
    source: str
    category: Text
    references: list[str]
    output: str
    path: list[str] = Field(default_factory=list)
    before: str | None = None
    term: bool = False
    rules: dict = Field(default_factory=dict)
    reuse: str | None = None

    _output = field_validator("output")(output_path)
    _path = field_validator("path")(namespace_path)

    @property
    def key(self) -> tuple[str, tuple[str, ...], str]:
        return self.output, tuple(self.path), self.source

    @model_validator(mode="after")
    def exact_source_key(self):
        if not self.source.strip():
            raise ValueError("Source text must not be blank")
        Rules.model_validate(self.rules)
        return self


class Plan(StrictModel):
    version: PlanVersion
    id: Text
    project: Text
    project_name: Text
    source_language: Text
    language: Text
    language_name: Text
    rules: dict
    style: str
    source_version: Text
    source_files: list[str]
    resources: dict[str, str]
    term_dictionaries: list[TermSource] = Field(default_factory=list)
    term_sources: list[TermSource] = Field(default_factory=list)
    tasks: list[Task]

    @property
    def dictionaries(self) -> list[TermSource]:
        return list(
            {
                (source.file, tuple(source.path)): source
                for source in [*self.term_sources, *self.term_dictionaries]
            }.values()
        )

    @model_validator(mode="after")
    def unique_tasks(self):
        ids = [task.id for task in self.tasks]
        keys = [task.key for task in self.tasks]
        if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise ValueError("Duplicate task or output/source key in plan")
        for task in self.tasks:
            if (
                not task.references
                or len(set(task.references)) != len(task.references)
                or set(task.references) - self.resources.keys()
            ):
                raise ValueError("Invalid task resource references")
        validate_locations(
            [
                *((task.output, task.path) for task in self.tasks),
                *((source.file, source.path) for source in self.dictionaries),
            ]
        )
        return self


class Translation(StrictModel):
    id: Text
    translation: str


class Submission(StrictModel):
    translations: list[Translation]


class ResolvedTerm(StrictModel):
    translation: Text
    note: str = ""
    categories: list[Text] = Field(default_factory=list)

    @field_validator("categories")
    @classmethod
    def ordered_categories(cls, value):
        return sorted(set(value))


type TermIndex = dict[str, list[ResolvedTerm]]


class Results(Submission):
    version: PlanVersion
    plan: Text
    project: Text
    language: Text
    glossary_before: Text
    published_terms_after: Text
