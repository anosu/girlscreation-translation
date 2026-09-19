"""Prove cache invalidation and interrupted-fetch behavior without CDN access."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adapters.girlscreation import collect
from adapters.girlscreation.fetch import fetch_sources
from workflow.adapters import CollectRequest
from workflow.utils import read_json, write_json


class FetchTests(unittest.TestCase):
    def test_daily_fetch_uses_publications_even_with_an_empty_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache, chinese, spanish = (root / name for name in ("sources", "zh", "es"))
            write_json(chinese / "novels/12345.json", {"本文": "现有译文"})
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
                downloads.append(Path(path).stem)
                return SimpleNamespace(content=Path(path).stem.encode())

            with (
                patch("adapters.girlscreation.fetch.request", side_effect=request),
                patch(
                    "adapters.girlscreation.fetch.text_assets",
                    return_value={"mItems": b"master"},
                ),
                patch(
                    "adapters.girlscreation.fetch.decrypt_master_text",
                    return_value=[],
                ),
                patch(
                    "adapters.girlscreation.fetch.parse_bundle",
                    side_effect=lambda raw: (
                        raw.decode(),
                        "title,題名,\nmessage,話者,本文,",
                    ),
                ),
            ):
                index = fetch_sources(cache, translations=[chinese])
                self.assertEqual(downloads, ["master", "mas_12346"])
                self.assertEqual(index["fetched_novels"], ["12346"])
                self.assertFalse((cache / "novels/12345.json").exists())
                write_json(chinese / "novels/12346.json", {"本文": "新译文"})
                assets[0]["h"] = "updated"
                downloads.clear()
                fetch_sources(cache, translations=[chinese])
                self.assertEqual(downloads, [])
                write_json(spanish / "novels/12346.json", {"本文": "Traducción"})
                fetch_sources(cache, translations=[chinese, spanish])
                self.assertEqual(downloads, ["mas_12345"])
                downloads.clear()
                fetch_sources(cache, translations=[chinese], check_existing=True)
                self.assertCountEqual(downloads, ["mas_12345", "mas_12346"])

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
                patch("adapters.girlscreation.fetch.request", side_effect=request),
                patch("adapters.girlscreation.fetch.parse_bundle", side_effect=parse),
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

    def test_failed_fetch_blocks_offline_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            with patch(
                "adapters.girlscreation.fetch.request",
                side_effect=ValueError("download failed"),
            ):
                with self.assertRaisesRegex(ValueError, "download failed"):
                    fetch_sources(cache)
            with self.assertRaisesRegex(ValueError, "interrupted"):
                list(collect(CollectRequest(cache, {"input": "."}, cache, None, {})))


if __name__ == "__main__":
    unittest.main()
