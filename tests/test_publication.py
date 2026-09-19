"""Manifest staging, strict dictionary format, and deterministic validation regressions."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow.build import build_staged, obj_hash, process
from workflow.dictionaries import read_dictionary
from workflow.utils import read_json, write_json
from workflow.validate import validate_translation


class PublicationTests(unittest.TestCase):
    def test_partial_staging_does_not_include_unstaged_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args], capture_output=True, check=True
                ).stdout

            git("init", "--quiet")
            git("config", "core.autocrlf", "false")
            source = root / "translations/zh-Hans/names.json"
            write_json(source, {"原文": "暂存"})
            git("add", "translations")
            write_json(source, {"原文": "未暂存"})
            write_json(source.parent / "untracked.json", {"未跟踪": "保留"})
            build_staged(root)
            manifest = json.loads(git("show", ":translations/zh-Hans/manifest.json"))
            self.assertEqual(manifest["names"], obj_hash({"原文": "暂存"}))
            self.assertNotIn("untracked", manifest)
            self.assertEqual(read_json(source), {"原文": "未暂存"})
            write_json(source.parent / "manifest.json", {"manual": "edit"})
            with self.assertRaisesRegex(ValueError, "Stage or restore"):
                build_staged(root)

    def test_custom_staged_output_paths_and_manifest_stability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            write_json(root / "assets/[es]/ui.json", {"Start": "Iniciar"})
            subprocess.run(
                ["git", "-C", str(root), "add", "."], check=True, capture_output=True
            )
            build_staged(root, {"es": "assets/[es]"})
            self.assertEqual(
                read_json(root / "assets/[es]/manifest.json")["ui"],
                obj_hash({"Start": "Iniciar"}),
            )
            before = (root / "assets/[es]/manifest.json").read_bytes()
            process(root / "assets/[es]")
            self.assertEqual((root / "assets/[es]/manifest.json").read_bytes(), before)

    def test_duplicate_json_keys_and_nested_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dictionary.json"
            path.write_text('{"原文":"甲","原文":"乙"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                read_json(path)
            write_json(path, {"menu": {"start": "开始"}})
            with self.assertRaisesRegex(ValueError, "flat"):
                read_dictionary(path)

    def test_tags_placeholders_and_line_breaks(self):
        rules = {
            "preserve_tags": True,
            "preserve_newlines": True,
            "protected_patterns": [r"\{[^{}]+\}"],
        }
        validate_translation("<b>{player}</b>\nはい", "<b>{player}</b>\n是", rules)
        for translation in ("{player}\n是", "<b>{wrong}</b>\n是", "<b>{player}</b>是"):
            with self.assertRaises(ValueError):
                validate_translation("<b>{player}</b>\nはい", translation, rules)
