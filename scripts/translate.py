"""Local entry point for the same tool-using agent run by codex-action in CI."""

import hashlib
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import TextIO

from scripts.codex import overrides, settings
from scripts.config import ROOT, Backend
from scripts.prepare import runtime_paths
from scripts.session import Session, setup_session


def protected_state(work: Path) -> dict[str, str]:
    """Fingerprint publication inputs and code before and after the local agent runs."""
    paths = runtime_paths(work)
    files = {
        work / "plan.json",
        work / "session.json",
        work / "runtime.json",
        paths["glossary"],
        paths["project_config"],
    }
    directories = [
        ROOT / name
        for name in ("scripts", "tests", "config", "prompts", ".github", "glossary")
    ]
    for directory in [*directories, paths["translations"], paths["sources"]]:
        if directory.exists():
            files.update(
                path
                for path in directory.rglob("*")
                if path.is_file()
                and path.suffix in {".py", ".json", ".md", ".toml", ".yml", ".yaml"}
            )
    files.update(
        ROOT / name
        for name in (
            "README.md",
            "package.json",
            "package-lock.json",
            "pyproject.toml",
            "uv.lock",
        )
    )
    result = {}
    for path in files:
        if path.exists():
            with path.open("rb") as stream:
                result[str(path.resolve())] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def codex_command(backend: Backend, work: Path) -> list[str]:
    """Run one agent in the repository, with shell tools and durable queue access."""
    node = shutil.which("node")
    cli = ROOT / "node_modules/@openai/codex/bin/codex.js"
    if not node or not cli.exists():
        raise ValueError("Install Node.js and run npm ci first")
    backend.require_model()
    command = [
        node,
        str(cli),
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(ROOT),
        "--add-dir",
        str(work.resolve()),
        "--model",
        backend.model,
        "--output-last-message",
        str(work.resolve() / "agent-report.md"),
        "--color",
        "never",
    ]
    config = {
        **settings(backend),
        "model_provider": "translation",
        "model_providers": {
            "translation": {
                "name": "Translation backend",
                "base_url": backend.base_url,
                "env_key": backend.api_key_env,
                "wire_api": "responses",
                "requires_openai_auth": False,
                "supports_websockets": False,
            }
        },
        "projects": {str(ROOT): {"trust_level": "trusted"}},
    }
    if os.name == "nt":
        config["windows"] = {"sandbox": "unelevated"}
    command.extend(overrides(config))
    return [*command, "-"]


def execute_codex(command: list[str], prompt: str, log: TextIO, timeout: int) -> None:
    """Bound the CLI process tree while preserving already submitted queue answers."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)


def translate_plan(
    work: Path, backend: Backend, timeout: int = 3600, *, session: Session | None = None
) -> None:
    """Let the agent inspect, translate and repair; independently require a complete queue."""
    session = session or setup_session(work)
    if session.status()["remaining"]:
        backend.require_model()
        if not os.environ.get(backend.api_key_env):
            raise ValueError(
                f"Set {backend.api_key_env} before running the translation agent"
            )
        command = codex_command(backend, work)
        prompt = (work / "agent-prompt.md").read_text(encoding="utf-8")
        log_path = work / "agent.log"
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            log_path.replace(work / "agent.previous.log")
        print(f"Running translation agent; log: {log_path}", flush=True)
        before = protected_state(work)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                execute_codex(command, prompt, log, timeout)
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"Translation agent exceeded {timeout}s; see {log_path}. Accepted answers are saved; rerun translate to continue."
            ) from error
        except subprocess.CalledProcessError as error:
            raise ValueError(
                f"Translation agent exited with code {error.returncode}; see {log_path}. Accepted answers are saved; rerun translate after resolving the error."
            ) from error
        finally:
            if protected_state(work) != before:
                raise ValueError(
                    "Agent modified protected inputs or code; review the changes before continuing"
                )
    session.refresh_answers()
    session.finalize()
