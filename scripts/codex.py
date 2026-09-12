"""The Codex settings shared by local CLI execution and the official Action."""

from pathlib import Path

import tomli_w

from scripts.config import ROOT, Backend


def settings(backend: Backend) -> dict:
    value = {
        "model_instructions_file": str(ROOT / "prompts/agent-system.md"),
        "model_reasoning_summary": "none",
        "web_search": "disabled",
        "approval_policy": "never",
        "features": {
            "shell_tool": True,
            "multi_agent": False,
            "plugins": False,
            "apps": False,
            "hooks": False,
        },
    }
    if backend.context_window is not None:
        value["model_context_window"] = backend.context_window
    if backend.effort:
        value["model_reasoning_effort"] = backend.effort
    return value


def configure_action(home: Path, backend: Backend) -> None:
    backend.require_model()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        tomli_w.dumps(settings(backend)), encoding="utf-8"
    )


def overrides(value: dict, prefix: tuple[str, ...] = ()) -> list[str]:
    """Serialize leaf overrides with TOML's own escaping, including dotted key names."""
    result = []
    for key, item in value.items():
        parts = (*prefix, key)
        if isinstance(item, dict):
            result.extend(overrides(item, parts))
        else:
            name = ".".join(tomli_w.dumps({part: 0}).split(" = ")[0] for part in parts)
            literal = tomli_w.dumps({"value": item}).partition(" = ")[2].strip()
            result.extend(["-c", f"{name}={literal}"])
    return result
