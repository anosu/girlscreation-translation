"""Create project data and configuration without copying the framework."""

from pathlib import Path

import tomli_w

from workflow.build import make_manifest
from workflow.config import Configuration


def create_project(
    directory: Path,
    *,
    project_id: str | None = None,
    name: str | None = None,
    source_language: str = "ja",
    targets: list[str] | None = None,
) -> Path:
    """Initialize a new directory; reject invalid settings before writing any files."""
    directory = directory.resolve()
    project_id = project_id or directory.name
    locales = [
        part.strip() for value in (targets or ["zh-Hans"]) for part in value.split(",")
    ]
    if len(set(locales)) != len(locales):
        raise ValueError("Target languages must be unique")
    config = {
        "schema_version": 1,
        "project": {
            "id": project_id,
            "name": name or project_id,
            "source_language": source_language,
            "backend": "compatible",
        },
        "adapter": {"input": "sources/resources.json"},
        "backends": {
            "compatible": {
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "MODEL_API_KEY",
            }
        },
        "targets": {code: {"style": f"styles/{code}.md"} for code in locales},
    }
    Configuration.model_validate(config)
    files = {
        "translation.toml": tomli_w.dumps(config),
        "sources/resources.json": "[]\n",
        "README.md": (
            f"# {name or project_id}\n\n"
            "1. 在 sources/resources.json 放入游戏原文资源；可以是一份资源或资源数组。\n"
            "2. 在 styles/ 和 glossary/ 维护风格与术语。\n"
            "3. 先同步并离线规划；准备实际翻译时再配置 translation.toml 的模型和密钥。\n\n"
            "最小文本资源示例（替换为自己的原文）：\n\n"
            '```json\n{"output":"ui.json","kind":"text","blocks":[{"texts":["はじめる"]}]}\n```\n\n'
            "从框架仓库根目录运行，将 CONFIG 替换为本目录 translation.toml 的路径：\n\n"
            "```sh\nnpm run workflow -- sync --config CONFIG\n"
            "npm run workflow -- plan --config CONFIG\n"
            "npm run workflow -- translate --config CONFIG\n"
            "npm run workflow -- publish --config CONFIG\n```\n\n"
            "id 和根字典 path 可省略。多个源文件可将 adapter.input 设置为 sources 目录。\n"
            '已有名称字典可在目标配置中设置 term_sources = [{ file = "names.json" }]。\n'
            "缓存自动隔离到本项目目录；升级后提示旧计划时，在原目录重新 plan 即可，无需搬译文。\n"
        ),
    }
    for code in locales:
        files[f"glossary/{code}.json"] = "{}\n"
        files[f"styles/{code}.md"] = (
            f"# Translation style: {code}\n\n"
            "Translate faithfully and naturally. Use the supplied scene and character context.\n"
            "Follow the glossary and preserve formatting required by entry rules.\n"
            "Add project-specific voice, terminology and UI requirements here.\n"
        )
        files[f"translations/{code}/manifest.json"] = make_manifest({}).decode("utf-8")
    # An existing directory may contain user data, even when no configuration exists.
    directory.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory / "translation.toml"
