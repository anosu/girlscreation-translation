"""Resource synchronization, offline planning, agent translation and dictionary publication."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from workflow.build import process
from workflow.config import (
    DEFAULT_CONFIG,
    ROOT,
    Project,
    Target,
    load_project,
    nonempty,
)
from workflow.dictionaries import dictionary_at, read_document
from workflow.glossary import project_terms, resolve_glossary
from workflow.merge import apply_updates, prepare_update
from workflow.operations import run_summary, status
from workflow.prepare import bind_runtime, prepare_tasks
from workflow.session import setup_session
from workflow.snapshot import read_resource, read_snapshot, sync_sources
from workflow.translate import DEFAULT_AGENT_TIMEOUT, translate_plan
from workflow.utils import unique_object
from workflow.validate import (
    changed_translations,
    combine_rules,
    validate_published,
    validate_translation,
)


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return number


def check_translations(
    project: Project, target: Target, changed_since: str | None = None
) -> None:
    names = project_terms(target.translations, target.term_sources)
    terms = resolve_glossary(target.glossary, names)
    count = 0
    documents = {}
    existing_paths, modified_paths = set(), set()
    if changed_since:
        subprocess.run(
            ["git", "rev-parse", "--verify", f"{changed_since}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        for command, paths in (
            (
                [
                    "ls-tree",
                    "-r",
                    "--name-only",
                    "-z",
                    changed_since,
                    "--",
                    str(target.translations),
                ],
                existing_paths,
            ),
            (
                [
                    "diff",
                    "--name-only",
                    "-z",
                    changed_since,
                    "--",
                    str(target.translations),
                ],
                modified_paths,
            ),
        ):
            result = subprocess.run(
                ["git", *command], cwd=ROOT, capture_output=True, check=True
            )
            paths.update(result.stdout.decode("utf-8").split("\0"))
    for path in target.translations.rglob("*.json"):
        if path.name == "manifest.json":
            continue
        document = read_document(path)
        if changed_since:
            relative = path.resolve().relative_to(ROOT).as_posix()
            if relative in existing_paths and relative not in modified_paths:
                continue
            previous = subprocess.run(
                ["git", "show", f"{changed_since}:{relative}"],
                cwd=ROOT,
                capture_output=True,
            )
            before = (
                json.loads(previous.stdout, object_pairs_hook=unique_object)
                if previous.returncode == 0
                else {}
            )
            document = changed_translations(document, before)
        documents[path.relative_to(target.translations).as_posix()] = document
        count += validate_published(document, target.rules)
    if (project.sources / "index.json").exists():
        snapshot, _, _ = read_snapshot(project)
        canonical = {
            (source.file, tuple(source.path)) for source in target.term_sources
        }
        for file in snapshot.resources.values():
            resource = read_resource(project.sources, file)
            dictionary = dictionary_at(
                documents.get(resource.output, {}), resource.path
            )
            category = (
                "terms"
                if resource.term or (resource.output, tuple(resource.path)) in canonical
                else resource.kind
            )
            rules = combine_rules(resource.rules, target.rules)
            for occurrence in resource.occurrences():
                source = occurrence["source"]
                if source in dictionary:
                    validate_translation(
                        source, dictionary[source], rules, category=category
                    )
    if target.translations.exists() and not process(target.translations, check=True):
        raise ValueError(
            f'Manifest is missing or outdated: {target.translations}. Run npm run build:manifest -- --config "{project.config}"'
        )
    print(
        f"{target.code}: {count} {'changed ' if changed_since else ''}dictionary keys, {len(terms)} terms; remote freshness and coverage not checked"
    )


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an independent game project")
    init.add_argument("directory", type=Path)
    init.add_argument("--id", dest="project_id")
    init.add_argument("--name")
    init.add_argument("--source-language", default="ja")
    init.add_argument("--target", action="append")
    for command in (
        "sync",
        "plan",
        "translate",
        "publish",
        "check",
        "update",
        "config",
        "status",
        "summary",
        "evaluate",
    ):
        sub = commands.add_parser(command)
        if command == "evaluate":
            sub.add_argument("--suite", type=Path, required=True)
            sub.add_argument("--answers", type=Path, required=True)
            sub.add_argument("--output", type=Path)
            continue
        sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        sub.add_argument("--target", action="append")
        if command == "check":
            sub.add_argument(
                "--changed-since",
                help="Check changed translations against a Git commit; structure and manifest checks still cover all files",
            )
        if command in {"sync", "update"}:
            sub.add_argument(
                "--source-id",
                action="append",
                help="Adapter-defined resource selection",
            )
        if command == "sync":
            sub.add_argument(
                "--export", action="store_true", help="Export selected snapshot for CI"
            )
        if command in {"plan", "update"}:
            sub.add_argument(
                "--limit",
                type=positive,
                help="Maximum pending resources; completed context is free and shared output files stay together. Does not limit acquisition.",
            )
        if command in {"translate", "update", "config"}:
            sub.add_argument("--backend", type=nonempty)
            sub.add_argument("--model", type=nonempty)
        if command in {"translate", "update"}:
            sub.add_argument(
                "--timeout",
                type=positive,
                default=DEFAULT_AGENT_TIMEOUT,
                help="Total timeout for the translation agent session",
            )
        if command == "summary":
            sub.add_argument("--output", type=Path)
    return parser


def main() -> None:
    parser = argument_parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            from workflow.scaffold import create_project

            path = create_project(
                args.directory,
                project_id=args.project_id,
                name=args.name,
                source_language=args.source_language,
                targets=args.target,
            )
            print(
                f"Created {path}. Fill sources/resources.json, then run sync and plan with --config."
            )
            return
        if args.command == "evaluate":
            from workflow.evaluation import evaluate

            result = evaluate(args.suite, args.answers, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["errors"]:
                parser.exit(1)
            return
        project = load_project(args.config)
        targets = project.select(args.target)
        if args.command == "config":
            records = []
            for target in targets:
                backend = (
                    project.backend(
                        target, args.backend, args.model, environ=os.environ
                    )
                    if any(
                        (
                            args.backend,
                            os.environ.get("TRANSLATION_BACKEND"),
                            target.backend,
                            project.project_backend,
                        )
                    )
                    else None
                )
                records.append(
                    {
                        "target": target.code,
                        "backend": backend.name if backend else None,
                        "model": backend.model if backend else None,
                        "backend_from": backend.selected_by if backend else None,
                        "model_from": backend.model_selected_by if backend else None,
                        "api_key_env": backend.api_key_env if backend else None,
                        "base_url": backend.base_url if backend else None,
                        "translations": str(target.translations),
                        "work": str(target.work),
                        "glossary": str(target.glossary),
                        "term_sources": [
                            source.model_dump() for source in target.term_sources
                        ],
                        "sources": str(project.sources),
                    }
                )
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return
        if args.command in {"status", "summary"}:
            if args.command == "summary":
                text = run_summary(project, targets)
                if args.output:
                    with args.output.open("a", encoding="utf-8") as stream:
                        stream.write(text)
                else:
                    print(text)
            else:
                print(
                    json.dumps(
                        [status(t) for t in targets],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return
        if args.command in {"sync", "update"}:
            snapshot = sync_sources(
                project,
                args.source_id,
                targets=targets,
                export=getattr(args, "export", False),
            )
            print(
                f"Collected {len(snapshot.resources)} resources; unselected history was not checked"
            )
        if args.command in {"plan", "update"}:
            for target in targets:
                prepare_tasks(project, target, args.limit)
        if args.command in {"translate", "update"}:
            for target in targets:
                bind_runtime(target.work, project, target)
                session = setup_session(target.work)
                if session.status()["remaining"]:
                    backend = project.backend(
                        target, args.backend, args.model, environ=os.environ
                    )
                    translate_plan(target.work, backend, args.timeout, session=session)
                else:
                    session.finalize()
        if args.command in {"publish", "update"}:
            updates = [prepare_update(project, target) for target in targets]
            print(f"Updated {apply_updates(updates)} publication files")
        if args.command in {"check", "update"}:
            for target in targets:
                check_translations(
                    project, target, getattr(args, "changed_since", None)
                )
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(
            1, f"Workflow failed: {error}\nUse status; accepted drafts are retained.\n"
        )


if __name__ == "__main__":
    main()
