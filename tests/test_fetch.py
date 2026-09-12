"""Prove cache invalidation and interrupted-fetch behavior without CDN access."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.config import load_project
from scripts.games.girlscreation.fetch import fetch_sources
from scripts.prepare import source_catalog
from scripts.utils import read_json, write_json


class FetchTests(unittest.TestCase):
    def test_hash_changes_and_partial_fetch_preserve_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            master = {"name": "master.dmm", "hash": "master-hash"}
            write_json(cache / "master.json", {})
            write_json(cache / "versions.json", {"master": master, "novels": {}})
            assets = [
                {"n": "notinit/novel_script/mas_12345.dmm", "h": "a"},
                {"n": "notinit/novel_script/mas_12346.dmm", "h": "b"},
            ]
            downloads = []

            def request(_client, path, _params=None):
                if path.endswith("/master.json"):
                    return SimpleNamespace(
                        json=lambda: {"d": [{"n": "master.dmm", "h": "master-hash"}]}
                    )
                if path.endswith("/assetbundle.json"):
                    return SimpleNamespace(json=lambda: {"d": assets})
                downloads.append(path)
                return SimpleNamespace(content=Path(path).stem.encode())

            def parse(content):
                return content.decode(), "title,題名,\nmessage,話者,本文,"

            with (
                patch("scripts.games.girlscreation.fetch.request", side_effect=request),
                patch(
                    "scripts.games.girlscreation.fetch.parse_bundle", side_effect=parse
                ),
            ):
                fetch_sources(cache)
                self.assertEqual(len(downloads), 2)
                fetch_sources(cache, ["12345"])
                self.assertEqual(
                    set(read_json(cache / "index.json")["novels"]), {"12345"}
                )
                fetch_sources(cache)
                self.assertEqual(len(downloads), 2)
                assets[1]["h"] = "updated"
                fetch_sources(cache)
                self.assertEqual(len(downloads), 3)
                self.assertIn("12346", downloads[-1])
            self.assertFalse((cache / ".fetching").exists())

    def test_failed_fetch_blocks_prepare(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            with patch(
                "scripts.games.girlscreation.fetch.request",
                side_effect=ValueError("download failed"),
            ):
                with self.assertRaisesRegex(ValueError, "download failed"):
                    fetch_sources(cache)
            with self.assertRaisesRegex(ValueError, "interrupted"):
                source_catalog(replace(load_project(), sources=cache))


if __name__ == "__main__":
    unittest.main()
