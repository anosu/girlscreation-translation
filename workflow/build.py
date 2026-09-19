"""Build the client's content hashes from working files or the Git index."""

import argparse
import hashlib
import json
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from workflow.config import DEFAULT_CONFIG, Configuration, load_project

ROOT = Path(__file__).resolve().parents[1]

SEPARATOR = b"\x00"
PATH_SEPARATOR = "\x01"


def traverse(obj: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """Flatten objects using the existing client hash protocol."""
    for key, value in sorted(obj.items()):
        if isinstance(value, dict):
            for sub_path, sub_value in traverse(value):
                yield f"{key}{PATH_SEPARATOR}{sub_path}", sub_value
        elif isinstance(value, str):
            yield key, value
        else:
            raise ValueError(
                f"Expected a string at {key!r}, got {type(value).__name__}"
            )


def obj_hash(obj: dict[str, Any]) -> str:
    """Hash content independently of JSON whitespace and object ordering."""
    md5 = hashlib.md5()

    for key, value in traverse(obj):
        md5.update(key.encode("utf-8"))
        md5.update(SEPARATOR)
        md5.update(value.encode("utf-8"))
        md5.update(SEPARATOR)

    return md5.hexdigest()


def file_hash(path: Path) -> str:
    """Return a translation file's content hash."""
    return obj_hash(json.loads(path.read_text(encoding="utf-8")))


def make_manifest(files: dict[str, bytes]) -> bytes:
    """Build a manifest from paths relative to a language directory."""
    manifest = {}
    for name, content in sorted(files.items()):
        if name == "manifest.json":
            continue
        parts = Path(name).with_suffix("").parts
        table = manifest
        for part in parts[:-1]:
            table = table.setdefault(part, {})
        table[parts[-1]] = obj_hash(json.loads(content))
    manifest["hash"] = obj_hash(manifest)
    return (
        json.dumps(manifest, sort_keys=True, indent=4, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def process(folder: Path, check: bool = False) -> bool:
    """Write a language manifest, or return whether it is up to date."""
    if not check:
        folder.mkdir(parents=True, exist_ok=True)
    files = {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*.json")
    }
    result = make_manifest(files)
    path = folder / "manifest.json"
    current = path.read_bytes() if path.exists() else None
    matches = current is not None and json.loads(current) == json.loads(result)
    if not check and current != result:
        path.write_bytes(result)
    return matches


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    """Run Git without a shell, including for arbitrary staged filenames."""
    return subprocess.run(
        ["git", "-C", str(root), *args], input=data, capture_output=True, check=True
    ).stdout


def build_staged(root: Path, folders: dict[str, str] | None = None) -> None:
    """Hash staged blobs only, without staging unstaged translation edits."""
    prefixes = list(folders.values()) if folders is not None else ["translations"]
    if not prefixes:
        return
    records = git(
        root, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", *prefixes
    ).split(b"\0")
    entries = []
    for record in filter(None, records):
        metadata, path = record.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        name = path.decode("utf-8")
        if not name.endswith(".json"):
            continue
        if stage != b"0" or mode not in (b"100644", b"100755"):
            raise ValueError(f"Unmerged or non-regular translation: {name}")
        entries.append((name, oid))
    if not entries:
        return
    output = git(
        root, "cat-file", "--batch", data=b"\n".join(oid for _, oid in entries) + b"\n"
    )
    offset = 0
    languages = {}
    for name, _ in entries:
        end = output.index(b"\n", offset)
        size = int(output[offset:end].split()[-1])
        content = output[end + 1 : end + 1 + size]
        offset = end + size + 2
        if folders is None:
            _, language, relative = name.split("/", 2)
        else:
            language = next(
                code
                for code, prefix in folders.items()
                if name.startswith(prefix + "/")
            )
            relative = name[len(folders[language]) + 1 :]
        languages.setdefault(language, {})[relative] = content
    updates = []
    for language, files in sorted(languages.items()):
        relative = f"{folders[language] if folders is not None else 'translations/' + language}/manifest.json"
        path = root / relative
        result = make_manifest(files)
        staged = files.get("manifest.json")
        current = path.read_bytes() if path.exists() else None
        normalized_current = (
            current.replace(b"\r\n", b"\n") if current is not None else None
        )
        normalized_staged = (
            staged.replace(b"\r\n", b"\n") if staged is not None else None
        )
        if normalized_current not in (normalized_staged, result):
            raise ValueError(
                f"Stage or restore your manifest edits before committing: {relative}"
            )
        if staged != result:
            updates.append((relative, result))
    for relative, result in updates:
        oid = (
            git(root, "hash-object", "-w", "--stdin", data=result)
            .strip()
            .decode("ascii")
        )
        git(root, "update-index", "--add", "--cacheinfo", "100644", oid, relative)
        (root / relative).write_bytes(result)
        print(f"Updated staged {relative}")


def main() -> None:
    """Build manifests for every language directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true")
    group.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    if args.staged:
        config_path = args.config.resolve()
        root = Path(
            git(config_path.parent, "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .strip()
        ).resolve()
        try:
            content = git(root, "show", ":" + config_path.relative_to(root).as_posix())
        except subprocess.CalledProcessError:
            parser.exit(
                1, "Stage the project configuration before building staged manifests.\n"
            )
        config = Configuration.model_validate(tomllib.loads(content.decode("utf-8")))
        folders = {}
        for code, target in config.targets.items():
            location = (
                config_path.parent / (target.translations or f"translations/{code}")
            ).resolve()
            if not location.is_relative_to(root) or location == root:
                parser.exit(
                    1,
                    f"Staged output directory must be inside the repository: {location}\n",
                )
            if any(
                location == other
                or location in other.parents
                or other in location.parents
                for other in ((root / path).resolve() for path in folders.values())
            ):
                parser.exit(1, "Staged output directories overlap.\n")
            folders[code] = location.relative_to(root).as_posix()
        build_staged(root, folders)
        return
    project = load_project(args.config)
    stale = [
        code
        for code, language in project.targets.items()
        if language.translations.is_dir()
        and not process(language.translations, args.check)
    ]
    if args.check and stale:
        parser.exit(1, f"Outdated manifests: {', '.join(stale)}\n")


if __name__ == "__main__":
    main()
