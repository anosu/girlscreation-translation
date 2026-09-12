"""A command-scoped translation session with typed plans and reusable projections."""

from functools import cached_property
from pathlib import Path

from scripts.adapters import load_adapter
from scripts.config import ROOT
from scripts.glossary import (
    apply_proposals,
    project_terms,
    read_optional,
    resolve_glossary,
    term_for,
    terms_payload,
)
from scripts.models import Answer, Results, Submission, Task, TermIndex, TermProposal
from scripts.prepare import read_plan, runtime_paths
from scripts.utils import digest, directory_digest, read_json, write_json
from scripts.validate import validate_results


def group_key(task: Task) -> tuple[bool, str, str]:
    return task.term, task.category, task.group


class Session:
    """Keep one consistent input snapshot during a command; persist accepted answers immediately."""

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
            raise ValueError("Source snapshot changed since prepare; run prepare again")
        self.base_glossary = read_optional(self.glossary_path)
        self.documents = {
            file: read_optional(self.translations / file)
            for file in {binding.target.file for binding in self.plan.term_bindings}
        }
        names, tables = self.projected_names({})
        terms = terms_payload(resolve_glossary(self.base_glossary, names, tables))
        if initialize:
            self.config = self._initialize(terms)
        else:
            self.config = read_json(self.work / "session.json")
            if self.plan.id != self.config["plan"]:
                raise ValueError("Plan changed; run setup again")
            if digest(terms) != self.config.get("terms_hash"):
                raise ValueError("Translation terminology changed; run setup again")

    def _initialize(self, terms: dict) -> dict:
        """Create session metadata and prompts from the inputs already loaded."""
        instructions = (ROOT / "prompts/agent.md").read_text(encoding="utf-8")
        system = (ROOT / "prompts/agent-system.md").read_text(encoding="utf-8")
        config = {
            "version": 7,
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
                    "rules": self.rules,
                    "style": self.plan.style,
                    "agent": instructions,
                    "system": system,
                }
            ),
            "terms_hash": digest(terms),
        }
        write_json(self.work / "session.json", config)
        (self.work / "style.md").write_text(self.plan.style, encoding="utf-8")
        prompt = (
            f"# Translation job: {self.plan.project_name}\nSource language: {self.plan.source_language}\n"
            f"Target language: {self.plan.language} ({self.plan.language_name})\n\n{instructions}\n\n"
            f"Work directory (pass --work to every agent command):\n{self.work}\n"
        )
        (self.work / "agent-prompt.md").write_text(prompt, encoding="utf-8")
        return config

    @cached_property
    def _answers(self) -> dict[str, str]:
        values = {}
        for path in (self.work / "answers").glob("*.json"):
            task = self.tasks.get(path.stem)
            if task is None:
                continue
            answer = Answer.model_validate(read_json(path))
            if answer.policy == self.config["policy"]:
                values.update(
                    validate_results(
                        [task],
                        {
                            "translations": [
                                {"id": answer.id, "translation": answer.translation}
                            ]
                        },
                        self.rules,
                    )
                )
        return values

    def answers(self) -> dict[str, str]:
        return dict(self._answers)

    def refresh_answers(self) -> None:
        """Reload after an external agent process has submitted new answers."""
        for key in ("_answers", "_proposals", "current_terms"):
            self.__dict__.pop(key, None)

    @cached_property
    def _proposals(self) -> list[TermProposal]:
        path = self.work / "proposals" / f"{self.plan.id}.json"
        return (
            [TermProposal.model_validate(item) for item in read_json(path)]
            if path.exists()
            else []
        )

    def proposals(self) -> list[TermProposal]:
        return list(self._proposals)

    def projected_names(self, values: dict[str, str]) -> tuple[dict, dict]:
        return project_terms(
            self.translations,
            self.plan.term_bindings,
            self.tasks,
            values,
            self.documents,
        )

    def terms(self, values: dict[str, str]) -> TermIndex:
        names, tables = self.projected_names(values)
        proposed = apply_proposals(
            self._proposals,
            self.tasks,
            values,
            self.base_glossary,
            names,
            tables,
            self.rules,
        )
        return resolve_glossary(proposed, names, tables)

    @cached_property
    def current_terms(self) -> TermIndex:
        return self.terms(self._answers)

    def validate(self, values: dict[str, str], terms: TermIndex | None = None) -> None:
        terms = terms if terms is not None else self.terms(values)
        for key, value in values.items():
            task = self.tasks[key]
            term = term_for(terms, task.source, task.category)
            if term and (task.term or task.use_terms) and value != term.translation:
                raise ValueError(
                    f"Use the established term for {task.source}: {term.translation}"
                )

    def status(self) -> dict:
        self.validate(self._answers, self.current_terms)
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

    def next_group(self) -> dict:
        status = self.status()
        terms_path = self.work / "terms.json"
        write_json(terms_path, terms_payload(self.current_terms))
        pending = [task for key, task in self.tasks.items() if key not in self._answers]
        if not pending:
            return {"remaining": 0, "next": None}
        key = group_key(pending[0])
        tasks = [task for task in pending if group_key(task) == key]
        group_path = self.work / "groups" / f"{digest(key)[:20]}.json"
        references = {
            str(self.translations / target.file)
            for task in tasks
            for target in task.targets
        }
        for task in tasks:
            for reference in task.references:
                path = (self.cache / reference).resolve()
                if not path.is_relative_to(self.cache.resolve()):
                    raise ValueError(
                        f"Adapter reference leaves source cache: {reference}"
                    )
                references.add(str(path))
        write_json(
            group_path,
            {
                "group": key[2],
                "tasks": [task.model_dump() for task in tasks],
                "references": sorted(references),
            },
        )
        return {
            "group": key[2],
            "count": len(tasks),
            "task_file": str(group_path),
            "terms_file": str(terms_path),
            "references": sorted(references),
            "remaining": status["remaining"],
        }

    def context(self, key: str) -> dict:
        return load_adapter(self.plan.adapter).context(
            self.tasks[key].model_dump(), self.cache, self.plan.adapter_options
        )

    def submit(self, payload: dict) -> dict:
        submission = Submission.model_validate(payload)
        if not submission.translations:
            raise ValueError("Provide a nonempty translations array")
        if any(item.id not in self.tasks for item in submission.translations):
            raise ValueError("Submission contains an unknown task ID")
        tasks = [self.tasks[item.id] for item in submission.translations]
        if any(
            task.term and key not in self._answers for key, task in self.tasks.items()
        ) and any(not task.term for task in tasks):
            raise ValueError("Complete and submit terminology tasks before other text")
        incoming = validate_results(tasks, payload, self.rules)
        combined = {**self._answers, **incoming}
        self.validate(combined)
        for key, value in incoming.items():
            write_json(
                self.work / "answers" / f"{key}.json",
                Answer(
                    policy=self.config["policy"], id=key, translation=value
                ).model_dump(),
            )
        self._answers.update(incoming)
        self.__dict__.pop("current_terms", None)
        return self.status()

    def propose(self, payload: list[dict]) -> dict:
        proposals = [TermProposal.model_validate(item) for item in payload]

        def identity(proposal: TermProposal):
            return proposal.source, tuple(sorted(proposal.categories))

        if len({identity(item) for item in proposals}) != len(proposals):
            raise ValueError("Duplicate proposed term scope")
        combined = {identity(item): item for item in self._proposals}
        combined.update({identity(item): item for item in proposals})
        names, tables = self.projected_names(self._answers)
        apply_proposals(
            list(combined.values()),
            self.tasks,
            self._answers,
            self.base_glossary,
            names,
            tables,
            self.rules,
        )
        write_json(
            self.work / "proposals" / f"{self.plan.id}.json",
            [item.model_dump() for item in combined.values()],
        )
        self.__dict__["_proposals"] = list(combined.values())
        self.__dict__.pop("current_terms", None)
        return {"proposals": len(combined)}

    def finalize(self) -> dict:
        if (
            directory_digest(self.cache, self.plan.source_files)
            != self.plan.source_version
        ):
            raise ValueError("Source snapshot changed since prepare")
        status = self.status()
        if status["remaining"]:
            raise ValueError(
                f"{status['remaining']} tasks remain; continue agent next/submit"
            )
        names, tables = self.projected_names(self._answers)
        after = apply_proposals(
            self._proposals,
            self.tasks,
            self._answers,
            self.base_glossary,
            names,
            tables,
            self.rules,
        )
        result = Results.model_validate(
            {
                "version": 7,
                "plan": self.plan.id,
                "project": self.plan.project,
                "language": self.plan.language,
                "translations": [
                    {"id": key, "translation": self._answers[key]} for key in self.tasks
                ],
                "glossary_before": digest(self.base_glossary),
                "glossary_after": digest(after),
                "published_terms_after": digest([names, tables]),
                "glossary_proposals": self._proposals,
            }
        )
        write_json(self.work / "results.json", result.model_dump())
        return status


def setup_session(work: Path) -> Session:
    session = Session(work, initialize=True)
    for task in session.plan.tasks:
        if task.id in session._answers or task.reuse is None:
            continue
        try:
            incoming = validate_results(
                [task],
                {"translations": [{"id": task.id, "translation": task.reuse}]},
                session.rules,
            )
            session.validate({**session._answers, **incoming})
        except ValueError:
            continue
        write_json(
            session.work / "answers" / f"{task.id}.json",
            Answer(
                policy=session.config["policy"], id=task.id, translation=task.reuse
            ).model_dump(),
        )
        session._answers.update(incoming)
    session.next_group()
    return session
