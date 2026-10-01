"""Validated, resumable answers for one translation plan."""

from functools import cached_property
from pathlib import Path

from workflow.config import ROOT
from workflow.dictionaries import read_document
from workflow.glossary import (
    project_terms,
    read_optional,
    resolve_glossary,
    term_for,
    terms_payload,
)
from workflow.models import Results, TermIndex
from workflow.prepare import read_plan, runtime_paths
from workflow.utils import digest, directory_digest, read_json, write_json
from workflow.validate import validate_results
from workflow.version import PLAN_VERSION


class Session:
    """Keep publication rules private; accept source-keyed batches from the Agent."""

    def __init__(self, work: Path, *, initialize: bool = False):
        self.work = work.resolve()
        self.plan = read_plan(self.work)
        self.tasks = {task.id: task for task in self.plan.tasks}
        self.rules = self.plan.rules
        paths = runtime_paths(self.work)
        self.translations, self.cache, self.glossary_path = (
            paths[key] for key in ("translations", "sources", "glossary")
        )
        if (
            initialize
            and directory_digest(self.cache, self.plan.source_files)
            != self.plan.source_version
        ):
            raise ValueError(
                "Source snapshot changed since prepare; run sync and plan again"
            )
        self.base_glossary = read_optional(self.glossary_path)
        self.documents = {
            file: read_document(self.translations / file)
            for file in {source.file for source in self.plan.dictionaries}
        }
        names = self.projected_names({})
        terms = terms_payload(resolve_glossary(self.base_glossary, names))
        if initialize:
            self.config = self._initialize(terms)
        else:
            self.config = read_json(self.work / "session.json")
            if self.plan.id != self.config["plan"]:
                raise ValueError(
                    "Plan changed; run translate to initialize this plan again"
                )
            if digest(terms) != self.config.get("terms_hash"):
                raise ValueError(
                    "Translation terminology changed; run translate to initialize this plan again"
                )

    def _initialize(self, terms: dict) -> dict:
        instructions = (ROOT / "workflow/prompts/agent.md").read_text(encoding="utf-8")
        config = {
            "version": PLAN_VERSION,
            "project": self.plan.project,
            "language": self.plan.language,
            "plan": self.plan.id,
            "policy": digest(
                {
                    "identity": [
                        self.plan.project,
                        self.plan.source_language,
                        self.plan.language,
                    ],
                    "terms": terms,
                    "term_sources": [
                        source.model_dump() for source in self.plan.term_sources
                    ],
                    "rules": self.rules,
                    "style": self.plan.style,
                    "agent": instructions,
                }
            ),
            "terms_hash": digest(terms),
        }
        write_json(self.work / "session.json", config)
        prompt = (
            f"# Translation job: {self.plan.project_name}\n"
            f"Source language: {self.plan.source_language}\n"
            f"Target language: {self.plan.language} ({self.plan.language_name})\n\n"
            f"{instructions}\n\nWork directory: {self.work}\n"
            f"# Translation style\n{self.plan.style}\n"
        )
        (self.work / "agent-prompt.md").write_text(prompt, encoding="utf-8")
        return config

    @cached_property
    def _answers(self) -> dict[str, str]:
        path = self.work / "answers.json"
        if not path.exists():
            return {}
        record = read_json(path)
        if not isinstance(record, dict):
            raise ValueError("Invalid saved translation answers")
        if record.get("policy") != self.config["policy"]:
            return {}
        values = record.get("translations")
        if not isinstance(values, dict):
            raise ValueError("Invalid saved translation answers")
        values = {key: value for key, value in values.items() if key in self.tasks}
        return validate_results(
            [self.tasks[key] for key in values],
            {
                "translations": [
                    {"id": key, "translation": value} for key, value in values.items()
                ]
            },
            self.rules,
        )

    def answers(self) -> dict[str, str]:
        return dict(self._answers)

    def refresh_answers(self) -> None:
        for key in ("_answers", "current_terms"):
            self.__dict__.pop(key, None)

    def projected_names(self, values: dict[str, str]) -> dict[str, str]:
        return project_terms(
            self.translations,
            self.plan.dictionaries,
            self.tasks,
            values,
            self.documents,
        )

    def terms(self, values: dict[str, str]) -> TermIndex:
        names = self.projected_names(values)
        return resolve_glossary(self.base_glossary, names)

    @cached_property
    def current_terms(self) -> TermIndex:
        return self.terms(self._answers)

    def validate(self, values: dict[str, str]) -> None:
        terms = self.terms(values)
        for key, value in values.items():
            task = self.tasks[key]
            term = term_for(terms, task.source, task.category)
            if task.term and term and value != term.translation:
                raise ValueError(
                    f"Use the established term for {task.source}: {term.translation}"
                )

    def status(self) -> dict:
        self.validate(self._answers)
        return {
            "plan": self.plan.id,
            "total": len(self.tasks),
            "completed": len(self._answers),
            "remaining": len(self.tasks) - len(self._answers),
            "by_category": {
                category: sum(
                    t.category == category and key not in self._answers
                    for key, t in self.tasks.items()
                )
                for category in sorted({t.category for t in self.tasks.values()})
            },
        }

    def submit_resources(self, payload: dict) -> dict:
        """Accept {resource_id: {exact_source: translation}} in one atomic batch."""
        if not isinstance(payload, dict) or not payload:
            raise ValueError("Submit a nonempty resource: source-to-translation object")
        by_resource = {resource_id: {} for resource_id in self.plan.resources}
        for task in self.tasks.values():
            for resource_id in task.references:
                by_resource[resource_id][task.source] = task
        incoming: dict[str, str] = {}
        for resource_id, translations in payload.items():
            if (
                resource_id not in by_resource
                or not isinstance(translations, dict)
                or not translations
            ):
                raise ValueError(f"Invalid resource submission: {resource_id}")
            for source, translation in translations.items():
                task = by_resource[resource_id].get(source)
                if task is None:
                    raise ValueError(
                        f"Source is not pending in {resource_id}: {source}"
                    )
                if task.id in incoming and incoming[task.id] != translation:
                    raise ValueError(f"Conflicting translations for {source}")
                incoming[task.id] = translation
        validated = validate_results(
            [self.tasks[key] for key in incoming],
            {
                "translations": [
                    {"id": key, "translation": value} for key, value in incoming.items()
                ]
            },
            self.rules,
        )
        combined = {**self._answers, **validated}
        self.validate(combined)
        if combined != self._answers:
            for name in ("results.json", "publication.json"):
                (self.work / name).unlink(missing_ok=True)
        write_json(
            self.work / "answers.json",
            {"policy": self.config["policy"], "translations": combined},
        )
        self._answers.update(validated)
        self.__dict__.pop("current_terms", None)
        return self.status()

    def finalize(self) -> dict:
        if (
            directory_digest(self.cache, self.plan.source_files)
            != self.plan.source_version
        ):
            raise ValueError("Source snapshot changed since prepare")
        status = self.status()
        if status["remaining"]:
            raise ValueError(
                f"{status['remaining']} tasks remain; continue translating"
            )
        names = self.projected_names(self._answers)
        result = Results.model_validate(
            {
                "version": PLAN_VERSION,
                "plan": self.plan.id,
                "project": self.plan.project,
                "language": self.plan.language,
                "translations": [
                    {"id": key, "translation": self._answers[key]} for key in self.tasks
                ],
                "glossary_before": digest(self.base_glossary),
                "published_terms_after": digest(names),
            }
        )
        write_json(self.work / "results.json", result.model_dump())
        return status


def setup_session(work: Path) -> Session:
    session = Session(work, initialize=True)
    incoming = {}
    for task in session.plan.tasks:
        if task.id in session._answers or task.reuse is None:
            continue
        try:
            validated = validate_results(
                [task],
                {"translations": [{"id": task.id, "translation": task.reuse}]},
                session.rules,
            )
            session.validate({**session._answers, **incoming, **validated})
        except ValueError:
            continue
        incoming.update(validated)
    if incoming:
        session._answers.update(incoming)
        write_json(
            session.work / "answers.json",
            {"policy": session.config["policy"], "translations": session._answers},
        )
    return session
