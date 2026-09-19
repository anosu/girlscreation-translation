"""A command-scoped translation session with typed plans and reusable projections."""

import json
from functools import cached_property
from pathlib import Path

from workflow.config import ROOT
from workflow.dictionaries import dictionary_at, read_document
from workflow.glossary import (
    apply_proposals,
    project_terms,
    read_optional,
    resolve_glossary,
    term_for,
    terms_payload,
)
from workflow.models import (
    PLAN_VERSION,
    Answer,
    Results,
    Submission,
    Task,
    TermIndex,
    TermProposal,
)
from workflow.prepare import read_plan, runtime_paths
from workflow.snapshot import read_resource
from workflow.utils import digest, directory_digest, read_json, write_json
from workflow.validate import validate_results


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
        """Create session metadata and prompts from the inputs already loaded."""
        instructions = (ROOT / "workflow/prompts/agent.md").read_text(encoding="utf-8")
        system = (ROOT / "workflow/prompts/agent-system.md").read_text(encoding="utf-8")
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
        proposed = apply_proposals(
            self._proposals,
            self.tasks,
            values,
            self.base_glossary,
            names,
            self.rules,
        )
        return resolve_glossary(proposed, names)

    @cached_property
    def current_terms(self) -> TermIndex:
        return self.terms(self._answers)

    def validate(self, values: dict[str, str], terms: TermIndex | None = None) -> None:
        terms = terms if terms is not None else self.terms(values)
        for key, value in values.items():
            task = self.tasks[key]
            term = term_for(terms, task.source, task.category)
            if term and value != term.translation:
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

    def packet_tasks(self, packet: str) -> list[Task]:
        for group in dict.fromkeys(task.group for task in self.plan.tasks):
            if digest([self.plan.id, group]) == packet:
                return [task for task in self.plan.tasks if task.group == group]
        raise ValueError("Unknown or stale packet; run next again")

    def next_group(self) -> dict:
        pending = [task for task in self.plan.tasks if task.id not in self._answers]
        if not pending:
            return {"remaining": 0, "next": None}
        group = pending[0].group
        packet = digest([self.plan.id, group])
        tasks = self.packet_tasks(packet)
        materials = {reference for task in tasks for reference in task.references}
        resources = [
            key for key, file in self.plan.resources.items() if file in materials
        ]
        rendered = self.read_resource(group, packet=packet)
        return {
            "packet": packet,
            "resource": group,
            "related_resources": resources,
            "pending": [
                str(i)
                for i, task in enumerate(tasks, 1)
                if task.id not in self._answers
            ],
            "remaining": len(pending),
            "page": rendered,
        }

    def read_resource(
        self,
        resource_id: str,
        *,
        packet: str | None = None,
        offset: int = 0,
        limit: int = 12000,
    ) -> dict:
        if offset < 0 or not 1 <= limit <= 64000:
            raise ValueError(
                "Use offset >= 0 and a page limit between 1 and 64000 characters"
            )
        if resource_id not in self.plan.resources:
            raise ValueError("Unknown resource in this plan")
        resource = read_resource(self.cache, self.plan.resources[resource_id])
        tasks = self.packet_tasks(packet) if packet else []
        numbers = {task.key: (str(i), task) for i, task in enumerate(tasks, 1)}
        all_tasks = {task.key: task for task in self.plan.tasks}
        document = dictionary_at(
            read_document(self.translations / resource.output), resource.path
        )
        lines = [
            json.dumps(
                {
                    "resource": resource.id,
                    "kind": resource.kind,
                    "dictionary": {"file": resource.output, "path": resource.path},
                    "context": resource.context,
                    "rules": resource.rules,
                },
                ensure_ascii=False,
            )
        ]
        columns = ["position", "speaker", "source", "number", "state", "translation"]
        lines.append(json.dumps({"columns": columns}, ensure_ascii=False))
        previous_block = None
        for occurrence in resource.occurrences():
            block = occurrence.get("block")
            if block is not None and block != previous_block:
                context = resource.blocks[block - 1].context
                if context:
                    lines.append(
                        json.dumps(
                            {"block": block, "context": context}, ensure_ascii=False
                        )
                    )
                previous_block = block
            source = occurrence["source"]
            address = (resource.output, tuple(resource.path), source)
            number, task = numbers.get(address, (None, None))
            known_task = all_tasks.get(address)
            accepted = self._answers.get(known_task.id) if known_task else None
            lines.append(
                json.dumps(
                    [
                        f"{block}.{occurrence['line']}"
                        if block
                        else str(occurrence["line"]),
                        occurrence.get("speaker"),
                        source,
                        number,
                        "accepted" if accepted else "pending" if task else "context",
                        accepted or document.get(source),
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        # Shared context is shown once; even a single long line is pageable.
        text = "\n".join(lines)
        relevant = {
            source: values
            for source, values in self.current_terms.items()
            if source in text
        }
        terms = list(terms_payload(relevant).items())
        return {
            "resource": resource.id,
            "offset": offset,
            "text": text[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None,
            "terms": dict(terms[:50]) if offset == 0 else {},
            "more_terms": len(terms) > 50,
        }

    def search(self, query: str, offset: int = 0) -> dict:
        if not query or offset < 0:
            raise ValueError("Provide a nonempty query and nonnegative offset")
        matches = []
        for key, file in self.plan.resources.items():
            resource = read_resource(self.cache, file)
            for row in resource.occurrences():
                if query in json.dumps(row, ensure_ascii=False):
                    matches.append(
                        {
                            "resource": key,
                            **row,
                            "source": row["source"][:500],
                            "truncated": len(row["source"]) > 500,
                        }
                    )
        terms = list(
            terms_payload(
                {k: v for k, v in self.current_terms.items() if query in k}
            ).items()
        )
        return {
            "matches": matches[offset : offset + 20],
            "next_offset": offset + 20
            if offset + 20 < max(len(matches), len(terms))
            else None,
            "terms": dict(terms[offset : offset + 20]),
        }

    def submit_packet(self, packet: str, values: dict) -> dict:
        tasks = self.packet_tasks(packet)
        if not isinstance(values, dict) or not values:
            raise ValueError("Submit a nonempty number: translation object")
        numbered = {str(i): task for i, task in enumerate(tasks, 1)}
        if set(values) - set(numbered):
            raise ValueError("Unknown submission number")
        for number, value in values.items():
            previous = self._answers.get(numbered[number].id)
            if previous is not None and previous != value:
                raise ValueError(
                    "Conflicting accepted dictionary value; resolve the conflict explicitly"
                )
        return self.submit(
            {
                "translations": [
                    {"id": numbered[number].id, "translation": value}
                    for number, value in values.items()
                ]
            }
        )

    def finish_packet(self, packet: str) -> dict:
        tasks = self.packet_tasks(packet)
        if any(task.id not in self._answers for task in tasks):
            raise ValueError("Packet has unresolved translations")
        if (
            directory_digest(self.cache, self.plan.source_files)
            != self.plan.source_version
        ):
            raise ValueError("Source snapshot changed")
        self.validate(self._answers, self.current_terms)
        return {"packet": packet, "remaining": 0}

    def revise_packet(self, packet: str, values: dict) -> dict:
        tasks = {str(i): task for i, task in enumerate(self.packet_tasks(packet), 1)}
        if not isinstance(values, dict) or not values or set(values) - set(tasks):
            raise ValueError("Invalid revision numbers")
        incoming = []
        for number, value in values.items():
            if not isinstance(value, dict) or set(value) != {"before", "translation"}:
                raise ValueError("A revision requires before and translation")
            if (
                tasks[number].id not in self._answers
                or self._answers[tasks[number].id] != value["before"]
            ):
                raise ValueError("Accepted translation changed before revision")
            incoming.append(
                {"id": tasks[number].id, "translation": value["translation"]}
            )
        return self.submit({"translations": incoming})

    def propose_packet(self, packet: str, values: list[dict]) -> dict:
        tasks = {str(i): task for i, task in enumerate(self.packet_tasks(packet), 1)}
        proposals = []
        for item in values:
            if item.get("evidence") not in tasks:
                raise ValueError("Unknown evidence number in packet")
            proposals.append({**item, "evidence": tasks[item["evidence"]].id})
        return self.propose(proposals)

    def submit(self, payload: dict) -> dict:
        submission = Submission.model_validate(payload)
        if not submission.translations:
            raise ValueError("Provide a nonempty translations array")
        if any(item.id not in self.tasks for item in submission.translations):
            raise ValueError("Submission contains an unknown task ID")
        tasks = [self.tasks[item.id] for item in submission.translations]
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
        names = self.projected_names(self._answers)
        apply_proposals(
            list(combined.values()),
            self.tasks,
            self._answers,
            self.base_glossary,
            names,
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
        names = self.projected_names(self._answers)
        after = apply_proposals(
            self._proposals,
            self.tasks,
            self._answers,
            self.base_glossary,
            names,
            self.rules,
        )
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
                "glossary_after": digest(after),
                "published_terms_after": digest(names),
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
