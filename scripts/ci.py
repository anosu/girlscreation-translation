"""Configuration-driven GitHub matrix, artifact restoration and publication paths."""

import argparse
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path

from scripts.codex import configure_action
from scripts.config import DEFAULT_CONFIG, ROOT, Project, Target, load_project, nonempty
from scripts.prepare import bind_runtime, read_plan
from scripts.utils import read_json


def workspace_path(path: Path) -> str:
    """CI only publishes configuration-controlled paths inside its checkout."""
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError(f"CI path is outside the checkout: {path}")
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def matrix(
    project: Project,
    codes: list[str] | None = None,
    backend: str | None = None,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Emit provider metadata and secret names, never secret values."""
    codex_version = read_json(ROOT / "package-lock.json")["packages"][
        "node_modules/@openai/codex"
    ]["version"]
    rows = []
    for language in project.select(codes):
        provider = project.backend(language, backend, model, environ=environ)
        rows.append(
            {
                "language": language.code,
                "codex_version": codex_version,
                "backend": provider.name,
                "model": provider.model or "",
                "effort": provider.effort or "",
                "api_key_env": provider.api_key_env,
                "endpoint": provider.responses_url,
                "work": workspace_path(language.work),
            }
        )
    return {"include": rows}


def github_outputs(values: dict, path: Path) -> None:
    """Write multiline-safe outputs using a delimiter that cannot collide with content."""
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            delimiter = "output_" + uuid.uuid4().hex
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def restore_artifacts(project: Project, targets: list[Target], artifacts: Path) -> None:
    """Restore publication inputs; answer caches and diagnostics stay out of publish."""
    flat = (artifacts / "plan.json").is_file()
    if flat and len(targets) != 1:
        raise ValueError("A flat artifact directory requires exactly one target")
    for target in targets:
        directory = artifacts if flat else artifacts / f"translation-{target.code}"
        plan = read_plan(directory)
        if (plan.project, plan.language, plan.source_language) != (
            project.id,
            target.code,
            project.source_language,
        ):
            raise ValueError("Artifact belongs to another project or language")
        files = [
            directory / name
            for name in ("plan.json", "results.json", "prepare-report.json")
        ]
        if any(not path.is_file() for path in files):
            raise ValueError(f"Publication artifact is incomplete: {directory}")
        report = directory / "agent-report.md"
        if report.is_file():
            files.append(report)
        target.work.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copyfile(path, target.work / path.name)
        bind_runtime(target.work, project, target)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("matrix", "configure", "restore", "stage"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        sub.add_argument(
            "--target", action="append", help="Repeat or separate locales with commas"
        )
        if command in {"matrix", "configure"}:
            sub.add_argument("--backend", type=nonempty)
            sub.add_argument("--model", type=nonempty)
        if command == "matrix":
            sub.add_argument("--github-output", type=Path)
        elif command == "configure":
            sub.add_argument("--home", type=Path, required=True)
        elif command == "restore":
            sub.add_argument("--artifacts", type=Path, required=True)
    return parser


def main() -> None:
    """Support the reusable workflow without embedding game paths in YAML."""
    parser = argument_parser()
    args = parser.parse_args()
    project = load_project(args.config)
    codes = args.target
    selected = project.select(codes)
    if args.command == "matrix":
        value = matrix(project, codes, args.backend, args.model, environ=os.environ)
        print(json.dumps(value, ensure_ascii=False))
        if args.github_output:
            github_outputs(
                {
                    "matrix": json.dumps(value),
                    "project": project.id,
                    "sources": workspace_path(project.sources),
                    "source_bundle": workspace_path(project.source_bundle),
                    "catalog": workspace_path(project.catalog),
                    "catalog_dir": workspace_path(project.catalog.parent),
                },
                args.github_output,
            )
    elif args.command == "configure":
        if len(selected) != 1:
            parser.error("configure requires exactly one --target")
        configure_action(
            args.home,
            project.backend(selected[0], args.backend, args.model, environ=os.environ),
        )
    elif args.command == "restore":
        restore_artifacts(project, selected, args.artifacts)
    else:
        paths = [
            workspace_path(path)
            for language in selected
            for path in (language.translations, language.glossary)
            if path.exists()
        ]
        if paths:
            subprocess.run(
                ["git", "--literal-pathspecs", "add", "--", *paths],
                cwd=ROOT,
                check=True,
            )


if __name__ == "__main__":
    main()
