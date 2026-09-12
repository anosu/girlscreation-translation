"""Fetch versioned source snapshots without modifying published translations."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from scripts.games.girlscreation.adapter import select_novels
from scripts.games.girlscreation.crypto import decrypt_master_text, get_url_params
from scripts.games.girlscreation.parse import parse_bundle, parse_script, text_assets
from scripts.utils import read_json, write_json

BASE_URL = "https://cdn-r18.gc.dmmgames.com"
ASSET_PATH = "/secure/data/production/webgl/resources/"
NOVEL_PATTERN = re.compile(r"notinit/[^/]+/\w{3}_(\d{8}|\d{5,6})\.dmm$")


def request(
    client: httpx.Client, path: str, params: dict | None = None
) -> httpx.Response:
    """Retry transient CDN failures, and reject HTTP errors before parsing."""
    for attempt in range(4):
        try:
            response = client.get(BASE_URL + path, params=params)
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            retryable = not isinstance(
                error, httpx.HTTPStatusError
            ) or error.response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }
            if not retryable or attempt == 3:
                raise
            time.sleep(2**attempt)
    raise AssertionError("Unreachable")


def fetch_sources(
    cache: Path,
    novel_ids: list[str] | None = None,
    *,
    translations: list[Path] | None = None,
    check_existing: bool = False,
) -> dict[str, Any]:
    """Refresh source snapshots by CDN hash; a lost cache can be rebuilt safely."""
    index_path = cache / "index.json"
    versions_path = cache / "versions.json"
    previous = (
        read_json(versions_path)
        if versions_path.exists()
        else (read_json(index_path) if index_path.exists() else {})
    )
    previous.setdefault("novels", {})
    cache.mkdir(parents=True, exist_ok=True)
    incomplete = cache / ".fetching"
    incomplete.write_text(
        "Fetch in progress; rerun fetch if interrupted.\n", encoding="utf-8"
    )
    index = {"version": 1, "novels": {}}
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        master_asset = request(client, "/files/manifest/webgl/r18/master.json").json()[
            "d"
        ][0]
        master_version = {"name": master_asset["n"], "hash": master_asset["h"]}
        if (
            previous.get("master") != master_version
            or not (cache / "master.json").exists()
        ):
            path = ASSET_PATH + master_asset["n"]
            data = request(
                client, path, get_url_params(path, master_asset["h"])
            ).content
            master = {
                name: decrypt_master_text(content)
                for name, content in text_assets(data).items()
            }
            write_json(cache / "master.json", master)
            print(f"Fetched master ({len(master)} tables)", flush=True)
        index["master"] = master_version
        previous["master"] = master_version
        write_json(versions_path, previous)
        assets = request(client, "/files/manifest/webgl/r18/assetbundle.json").json()[
            "d"
        ]
        selected = set(novel_ids) if novel_ids else None
        novels = {}
        for asset in assets:
            match = NOVEL_PATTERN.fullmatch(asset["n"])
            if not match or (selected is not None and match[1] not in selected):
                continue
            novel_id = match[1]
            if novel_id in index["novels"]:
                raise ValueError(f"Multiple assets use novel ID {novel_id}")
            version = {"name": asset["n"], "hash": asset["h"]}
            index["novels"][novel_id] = version
            novels[novel_id] = asset
        if selected is not None and selected != set(index["novels"]):
            raise ValueError(
                f"Novel IDs not found: {sorted(selected - set(index['novels']))}"
            )
        wanted = select_novels(
            novels, translations, check_existing or selected is not None
        )
        index["fetched_novels"] = sorted(wanted)
        downloads = [
            (novel_id, novels[novel_id])
            for novel_id in sorted(wanted)
            if check_existing
            or previous["novels"].get(novel_id) != index["novels"][novel_id]
            or not (cache / "novels" / f"{novel_id}.json").exists()
        ]

        def download(item: tuple[str, dict]) -> str:
            novel_id, asset = item
            path = ASSET_PATH + asset["n"]
            name, script = parse_bundle(
                request(client, path, get_url_params(path, asset["h"])).content
            )
            if name != Path(asset["n"]).stem:
                raise ValueError(f"Novel asset name mismatch: {name}")
            write_json(
                cache / "novels" / f"{novel_id}.json",
                {"id": novel_id, "script": name, "messages": parse_script(script)},
            )
            return novel_id

        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                for number, novel_id in enumerate(executor.map(download, downloads), 1):
                    previous["novels"][novel_id] = index["novels"][novel_id]
                    if number % 50 == 0 or number == len(downloads):
                        write_json(versions_path, previous)
                        print(
                            f"Fetched {number}/{len(downloads)} changed novels",
                            flush=True,
                        )
        finally:
            write_json(versions_path, previous)
    # A partial fetch is explicit and prepare only visits the selected snapshots.
    index["partial"] = selected is not None
    write_json(index_path, index)
    incomplete.unlink()
    print(
        f"Source snapshot ready: {len(wanted)}/{len(index['novels'])} novels selected ({len(downloads)} downloaded)",
        flush=True,
    )
    return index
