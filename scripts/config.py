"""Strict project configuration and explicit per-run model selection."""

import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal, Mapping
from urllib.parse import urlparse

import langcodes
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from scripts.utils import read_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "translation.toml"


def nonempty(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("must be nonempty and have no surrounding whitespace")
    return value


def locale(value: str) -> str:
    if not langcodes.tag_is_valid(value) or langcodes.standardize_tag(value) != value:
        raise ValueError("use a canonical language tag, such as zh-Hans or pt-BR")
    return value


Text = Annotated[str, AfterValidator(nonempty)]
Identifier = Annotated[Text, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
Locale = Annotated[Text, AfterValidator(locale)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CodexSettings(StrictModel):
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    context_window: Annotated[int, Field(gt=0)] | None = None


class BackendSettings(StrictModel):
    base_url: Text
    api_key_env: Annotated[Text, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    model: Text | None = None
    codex: CodexSettings = Field(default_factory=CodexSettings)


class Rules(StrictModel):
    required_terms: dict[Text, Text] = Field(default_factory=dict)
    forbidden_translations: list[Text] = Field(default_factory=list)
    name_kinds: list[Text] = Field(default_factory=list)
    number_kinds: list[Text] = Field(default_factory=list)
    name_identifier_pattern: Text | None = None
    protected_patterns: list[Text] = Field(default_factory=list)
    preserve_tags: bool = False
    preserve_newlines: bool = False

    @model_validator(mode="after")
    def valid_patterns(self):
        for pattern in [
            *self.protected_patterns,
            *([self.name_identifier_pattern] if self.name_identifier_pattern else []),
        ]:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"Invalid validation pattern: {error}") from error
        return self


class TargetSettings(StrictModel):
    name: Text | None = None
    backend: Identifier | None = None
    translations: Text | None = None
    work: Text | None = None
    glossary: Text | None = None
    style: Text | None = None
    rules: Text | None = None


class ProjectSettings(StrictModel):
    id: Identifier
    name: Text | None = None
    source_language: Locale
    adapter: Text
    backend: Identifier | None = None
    sources: Text | None = None


class Configuration(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    project: ProjectSettings
    adapter: dict = Field(default_factory=dict)
    backends: dict[Identifier, BackendSettings] = Field(default_factory=dict)
    targets: Annotated[dict[Locale, TargetSettings], Field(min_length=1)]


@dataclass(frozen=True)
class Backend:
    name: str
    base_url: str
    api_key_env: str
    model: str | None = None
    effort: str | None = None
    context_window: int | None = None
    selected_by: str = "project.backend"
    model_selected_by: str = "backend.model"

    @property
    def responses_url(self) -> str:
        return self.base_url.rstrip("/") + "/responses"

    def require_model(self) -> None:
        if self.model is None:
            raise ValueError(f"Set backends.{self.name}.model or pass --model")


@dataclass(frozen=True)
class Target:
    code: str
    name: str
    backend: str | None
    translations: Path
    work: Path
    glossary: Path
    style: str
    rules: dict


@dataclass(frozen=True)
class Project:
    config: Path
    root: Path
    id: str
    name: str
    source_language: str
    adapter: str
    options: dict
    sources: Path
    targets: dict[str, Target]
    backends: dict[str, Backend]
    project_backend: str | None

    @property
    def catalog(self) -> Path:
        return self.sources.parent / f"{self.sources.name}.catalog.json"

    @property
    def source_bundle(self) -> Path:
        return self.sources.parent / f"{self.sources.name}.zip"

    def select(self, codes: list[str] | None = None) -> list[Target]:
        selected = (
            list(self.targets)
            if codes is None
            else [part.strip() for value in codes for part in value.split(",")]
        )
        if (
            not selected
            or any(not c for c in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ValueError(
                "Select at least one target, without empty values or duplicates"
            )
        unknown = set(selected) - self.targets.keys()
        if unknown:
            raise ValueError(f"Unknown targets: {', '.join(sorted(unknown))}")
        return [self.targets[c] for c in selected]

    def backend(
        self,
        target: Target,
        name: str | None = None,
        model: str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> Backend:
        """Resolve precedence once; callers explicitly supply their environment."""
        env = environ or {}
        choices = [
            (name, "--backend"),
            (env.get("TRANSLATION_BACKEND") or None, "TRANSLATION_BACKEND"),
            (target.backend, f"targets.{target.code}.backend"),
            (self.project_backend, "project.backend"),
        ]
        selected, origin = next(
            ((v, k) for v, k in choices if v is not None), (None, "")
        )
        if selected is None:
            raise ValueError(f"No backend selected for target {target.code}")
        nonempty(selected)
        if selected not in self.backends:
            raise ValueError(f"Unknown backend: {selected}")
        backend = self.backends[selected]
        models = [
            (model, "--model"),
            (env.get("TRANSLATION_MODEL") or None, "TRANSLATION_MODEL"),
            (backend.model, f"backends.{selected}.model"),
        ]
        value, model_origin = next(
            ((v, k) for v, k in models if v is not None), (None, "unset")
        )
        if value is not None:
            nonempty(value)
        return replace(
            backend, model=value, selected_by=origin, model_selected_by=model_origin
        )


def load_project(path: Path = DEFAULT_CONFIG) -> Project:
    """Read configuration without reading secrets or process environment."""
    path = path.resolve()
    try:
        config = Configuration.model_validate(
            tomllib.loads(path.read_text(encoding="utf-8"))
        )
    except ValidationError as error:
        raise ValueError(f"Invalid configuration {path}:\n{error}") from error
    root, settings = path.parent, config.project
    resolve = lambda value: (root / value).resolve()
    backends = {}
    for name, values in config.backends.items():
        url = urlparse(values.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                f"backends.{name}.base_url must be a plain HTTP API base URL without credentials"
            )
        backends[name] = Backend(
            name,
            values.base_url.rstrip("/"),
            values.api_key_env,
            values.model,
            values.codex.effort,
            values.codex.context_window,
        )
    for location, reference in [
        ("project.backend", settings.backend),
        *[(f"targets.{code}.backend", t.backend) for code, t in config.targets.items()],
    ]:
        if reference is not None and reference not in backends:
            raise ValueError(f"{location}: unknown backend {reference}")
    targets = {}
    for code, values in config.targets.items():
        rules = Rules.model_validate(
            read_json(resolve(values.rules)) if values.rules else {}
        ).model_dump(exclude_none=True)
        targets[code] = Target(
            code,
            values.name or code,
            values.backend,
            resolve(values.translations or f"translations/{code}"),
            resolve(values.work or f".cache/translation/{settings.id}/work-v7/{code}"),
            resolve(values.glossary or f"glossary/{code}.json"),
            resolve(values.style).read_text(encoding="utf-8")
            if values.style
            else "Translate faithfully and naturally into the specified target language.",
            rules,
        )
    sources = resolve(settings.sources or f".cache/translation/{settings.id}/sources")
    occupied = [
        ("project.sources", sources),
        ("project.catalog", sources.parent / f"{sources.name}.catalog.json"),
        ("project.source_bundle", sources.parent / f"{sources.name}.zip"),
    ]
    for code, target in targets.items():
        for role in ("translations", "work", "glossary"):
            location = getattr(target, role)
            for other_role, other in occupied:
                if (
                    location == other
                    or location in other.parents
                    or other in location.parents
                ):
                    raise ValueError(
                        f"Paths overlap: targets.{code}.{role} and {other_role}"
                    )
            occupied.append((f"targets.{code}.{role}", location))
    return Project(
        path,
        root,
        settings.id,
        settings.name or settings.id,
        settings.source_language,
        settings.adapter,
        config.adapter,
        sources,
        targets,
        backends,
        settings.backend,
    )
