"""User commands for preparing, translating and publishing a configured project."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from scripts.adapters import load_adapter
from scripts.build import process, traverse
from scripts.config import DEFAULT_CONFIG, Project, Target, load_project, nonempty
from scripts.glossary import project_terms, resolve_glossary
from scripts.merge import apply_updates, prepare_update
from scripts.models import Catalog
from scripts.operations import prune_cache, run_summary, status
from scripts.prepare import bind_runtime, compile_catalog, prepare_tasks, source_catalog
from scripts.session import Session, setup_session
from scripts.translate import translate_plan
from scripts.utils import read_json


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return number


def check_translations(project: Project, target: Target) -> None:
    if not target.translations.exists():
        print(f"{target.code}: no published translations yet")
        return
    adapter = load_adapter(project.adapter)
    catalog = adapter.publication(
        Catalog(entries=[]),
        target.translations,
        {**adapter.settings(project.options), "root": str(project.root)},
    )
    names, terms = project_terms(target.translations, catalog.term_bindings)
    resolved = resolve_glossary(target.glossary, names, terms)
    count = 0
    for path in target.translations.rglob("*.json"):
        if path.name == "manifest.json":
            continue
        for key, value in traverse(read_json(path)):
            if not value.strip() or any(
                word in value for word in target.rules.get("forbidden_translations", [])
            ):
                raise ValueError(f"Invalid published translation: {path}: {key}")
            count += 1
    if not process(target.translations, check=True):
        raise ValueError(f"Manifest is outdated: {target.translations}")
    print(
        f"{target.code}: publication structure and manifest checked ({count} translations, {len(resolved)} terms); source alignment not checked"
    )


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "fetch",
        "catalog",
        "prepare",
        "setup",
        "translate",
        "finalize",
        "merge",
        "check",
        "update",
        "config",
        "status",
        "cache",
        "summary",
        "evaluate",
    ):
        sub = commands.add_parser(command)
        if command != "evaluate":
            sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if command != "evaluate":
            sub.add_argument(
                "--target",
                action="append",
                help="Repeat or separate locales with commas; omitted selects all targets",
            )
        if command in {"fetch", "update"}:
            sub.add_argument("--source-id", action="append")
        if command in {"fetch", "catalog", "prepare", "update"}:
            sub.add_argument(
                "--check-existing",
                action="store_true",
                help="Also inspect previously published content",
            )
        if command == "catalog":
            sub.add_argument(
                "--export",
                action="store_true",
                help="Bundle only sources needed by the compiled catalog",
            )
        if command in {"prepare", "update"}:
            sub.add_argument(
                "--limit",
                type=positive,
                help="Maximum planned entries per target, including terminology",
            )
        if command == "prepare":
            sub.add_argument(
                "--catalog", type=Path, help="Use a previously compiled shared catalog"
            )
        if command in {"translate", "update", "config"}:
            sub.add_argument("--backend", type=nonempty)
            sub.add_argument("--model", type=nonempty)
        if command in {"translate", "update"}:
            sub.add_argument("--timeout", type=positive, default=3600)
        if command == "update":
            sub.add_argument(
                "--dry-run",
                action="store_true",
                help="Fetch and prepare; do not call models or publish",
            )
        if command == "cache":
            sub.add_argument("--prune", action="store_true", required=True)
            sub.add_argument("--days", type=positive, default=30)
        if command == "summary":
            sub.add_argument("--output", type=Path)
        if command == "evaluate":
            sub.add_argument("--suite", type=Path, required=True)
            sub.add_argument("--answers", type=Path, required=True)
            sub.add_argument("--output", type=Path)
    return parser


def main() -> None:
    parser = argument_parser()
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            from scripts.evaluation import evaluate

            result = evaluate(args.suite, args.answers, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["errors"]:
                parser.exit(1)
            return
        project = load_project(args.config)
        targets = project.select(getattr(args, "target", None))
        if args.command == "config":
            records = []
            for target in targets:
                backend = project.backend(
                    target, args.backend, args.model, environ=os.environ
                )
                records.append(
                    {
                        "target": target.code,
                        "backend": backend.name,
                        "backend_from": backend.selected_by,
                        "model": backend.model,
                        "model_from": backend.model_selected_by,
                        "api_key_env": backend.api_key_env,
                        "base_url": backend.base_url,
                        "codex": {
                            "effort": backend.effort,
                            "context_window": backend.context_window,
                        },
                        **{
                            key: str(getattr(target, key))
                            for key in ("translations", "work", "glossary")
                        },
                    }
                )
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return
        if args.command in {"status", "cache", "summary"}:
            if args.command == "summary":
                text = run_summary(project, targets)
                if args.output:
                    with args.output.open("a", encoding="utf-8") as stream:
                        stream.write(text)
                else:
                    print(text)
                return
            if args.command == "status":
                records = [status(target) for target in targets]
            else:
                records = [prune_cache(target, args.days) for target in targets]
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return
        adapter = load_adapter(project.adapter)
        options = adapter.settings(project.options)
        if args.command in {"fetch", "update"}:
            adapter.fetch(
                project.sources,
                args.source_id,
                {**options, "root": str(project.root)},
                translations=[target.translations for target in targets],
                check_existing=args.check_existing,
            )
        if args.command == "catalog":
            catalog = compile_catalog(
                project,
                targets=targets,
                check_existing=args.check_existing,
                export=args.export,
            )
            print(f"Compiled {len(catalog.entries)} entries: {project.catalog}")
            return
        if args.command in {"prepare", "update"}:
            catalog = source_catalog(
                project,
                getattr(args, "catalog", None),
                targets=targets,
                check_existing=args.check_existing,
            )
            for target in targets:
                prepare_tasks(
                    project,
                    target,
                    catalog,
                    args.limit,
                    check_existing=args.check_existing,
                )
            if args.command == "update" and args.dry_run:
                print("Dry run complete. No model calls or publication performed.")
                return
        for target in targets:
            if args.command == "setup":
                bind_runtime(target.work, project, target)
                setup_session(target.work)
            if args.command in {"translate", "update"}:
                bind_runtime(target.work, project, target)
                session = setup_session(target.work)
                if session.status()["remaining"]:
                    backend = project.backend(
                        target, args.backend, args.model, environ=os.environ
                    )
                    translate_plan(target.work, backend, args.timeout, session=session)
                else:
                    session.finalize()
            if args.command == "finalize":
                Session(target.work).finalize()
        if args.command in {"merge", "update"}:
            updates = [prepare_update(project, target) for target in targets]
            print(f"Updated {apply_updates(updates)} publication files")
        if args.command == "check" or (
            args.command == "update" and args.check_existing
        ):
            for target in targets:
                check_translations(project, target)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            f"Workflow failed: {error}\nUse status to inspect progress; accepted answers remain in the target work directory.\n",
        )


if __name__ == "__main__":
    main()
