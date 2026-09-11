import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

trans_dir = Path("translations")
languages = ["zh-Hans"]

SEPARATOR = b"\x00"
PATH_SEPARATOR = "\x01"


def traverse(obj: dict[str, Any]) -> Iterable[tuple[str, str]]:
    for key, value in sorted(obj.items()):
        if isinstance(value, dict):
            for sub_path, sub_value in traverse(value):
                yield f"{key}{PATH_SEPARATOR}{sub_path}", sub_value
        else:
            yield key, value


def obj_hash(obj: dict[str, Any]) -> str:
    md5 = hashlib.md5()

    for key, value in traverse(obj):
        md5.update(key.encode("utf-8"))
        md5.update(SEPARATOR)
        md5.update(value.encode("utf-8"))
        md5.update(SEPARATOR)

    return md5.hexdigest()


def file_hash(path: Path) -> str:
    return obj_hash(json.loads(path.read_text(encoding="utf-8")))


def process(folder: Path):
    manifest = {}
    manifest_path = folder / "manifest.json"

    for file in folder.rglob("*.json"):
        if manifest_path.samefile(file):
            continue
        parts = file.relative_to(folder).with_suffix("").parts
        table = manifest
        for part in parts[:-1]:
            table = manifest.setdefault(part, {})
        table[parts[-1]] = file_hash(file)

    manifest["hash"] = obj_hash(manifest)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    for lang in languages:
        process(trans_dir / lang)


if __name__ == "__main__":
    main()
