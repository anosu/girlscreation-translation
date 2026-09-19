"""Game parsing and staged manifest regressions retained during migration."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.girlscreation import source_text
from adapters.girlscreation.parse import parse_script, text_assets
from workflow.build import build_staged, obj_hash, process
from workflow.utils import read_json, write_json


class ManifestTests(unittest.TestCase):
    def test_configured_output_roots_and_literal_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            folders = {"es": "assets/[spanish]", "zh-Hans": "localization/chinese"}
            for prefix in folders.values():
                write_json(root / prefix / "ui.json", {"start": "value"})
            subprocess.run(
                ["git", "-C", str(root), "add", "."], check=True, capture_output=True
            )
            build_staged(root, folders)
            for prefix in folders.values():
                self.assertEqual(
                    read_json(root / prefix / "manifest.json")["ui"],
                    obj_hash({"start": "value"}),
                )

    def test_initial_manifest_nested_paths_and_stability(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            write_json(folder / "novels/extra/12345.json", {"原文": "译文"})
            self.assertFalse(process(folder))
            manifest = read_json(folder / "manifest.json")
            self.assertEqual(
                manifest["novels"]["extra"]["12345"], obj_hash({"原文": "译文"})
            )
            self.assertTrue(process(folder, check=True))
            before = (folder / "manifest.json").read_bytes()
            process(folder)
            self.assertEqual(before, (folder / "manifest.json").read_bytes())
            self.assertEqual(
                obj_hash({"b": "2", "a": "1"}), obj_hash({"a": "1", "b": "2"})
            )

    def test_partial_staging_and_manifest_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args], capture_output=True, check=True
                ).stdout

            git("init", "--quiet")
            git("config", "core.autocrlf", "false")
            source = root / "translations/zh-Hans/names.json"
            write_json(source, {"原文": "暂存译文"})
            git("add", "translations")
            write_json(source, {"原文": "未暂存译文"})
            write_json(source.parent / "untracked.json", {"新": "未跟踪"})
            build_staged(root)
            staged = json.loads(git("show", ":translations/zh-Hans/manifest.json"))
            self.assertEqual(staged["names"], obj_hash({"原文": "暂存译文"}))
            self.assertNotIn("untracked", staged)
            self.assertEqual(read_json(source), {"原文": "未暂存译文"})
            self.assertEqual(
                json.loads(git("show", ":translations/zh-Hans/names.json")),
                {"原文": "暂存译文"},
            )
            manifest = source.parent / "manifest.json"
            # Git's Windows checkout conversion is not a manual manifest edit.
            manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
            build_staged(root)
            write_json(manifest, {"manual": "edit"})
            with self.assertRaisesRegex(ValueError, "Stage or restore"):
                build_staged(root)
            self.assertEqual(read_json(manifest), {"manual": "edit"})


class ParsingTests(unittest.TestCase):
    def test_story_commands_and_context(self):
        script = 'title,題名,\nmessage,,"正文<br>{0}",\nmsgvoicesync,chara_9,話者,台詞,voice\nwait,1'
        rows = parse_script(script)
        self.assertEqual([r["kind"] for r in rows], ["title", "message", "message"])
        self.assertEqual(rows[1]["message"], '"正文<br>{0}"')
        self.assertEqual(rows[2]["name"], "話者")
        self.assertEqual(rows[2]["line"], 3)
        with self.assertRaises(ValueError):
            parse_script("msgvoicesync,broken")

    def test_unitypy_binary_surrogateescape(self):
        from types import SimpleNamespace

        content = b"hello\xff\x80"
        asset = SimpleNamespace(
            m_Name="master", m_Script=content.decode("utf-8", "surrogateescape")
        )
        obj = SimpleNamespace(
            type=SimpleNamespace(name="TextAsset"), read=lambda: asset
        )
        with patch(
            "adapters.girlscreation.parse.UnityPy.load",
            return_value=SimpleNamespace(objects=[obj]),
        ):
            self.assertEqual(text_assets(b"bundle"), {"master": content})

    def test_master_language_types(self):
        self.assertEqual(source_text(["日本語", "English"], "array"), "日本語")
        self.assertEqual(source_text("日本語|English", "strings"), "日本語")
        self.assertIsNone(source_text([], "array"))
        with self.assertRaises(ValueError):
            source_text("invalid", "array")

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text('{"key":"one","key":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                read_json(path)
