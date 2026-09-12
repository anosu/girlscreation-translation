"""Regression tests for source extraction, staged hashes and safe publication."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.build import ROOT, build_staged, obj_hash, process
from scripts.config import load_project
from scripts.games.girlscreation.adapter import source_text
from scripts.games.girlscreation.parse import parse_script, text_assets
from scripts.glossary import resolve_glossary
from scripts.merge import merge_results
from scripts.models import Task
from scripts.prepare import prepare_tasks
from scripts.session import Session, setup_session
from scripts.translate import translate_plan
from scripts.utils import read_json, write_json
from scripts.validate import PROTECTED, validate_results, validate_translation


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
            "scripts.games.girlscreation.parse.UnityPy.load",
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


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "sources"
        self.work = self.root / "work"
        self.translations = self.root / "translations/zh-Hans"
        self.project = replace(load_project(), sources=self.cache)
        self.target = replace(
            self.project.targets["zh-Hans"],
            translations=self.translations,
            work=self.work,
            glossary=self.root / "glossary.json",
        )
        write_json(self.target.glossary, read_json(ROOT / "glossary/zh-Hans.json"))
        write_json(
            self.translations / "names.json",
            read_json(ROOT / "translations/zh-Hans/names.json"),
        )
        write_json(
            self.translations / "master.json",
            {"mNovels": {"title": {"題名": "旧标题"}}},
        )
        write_json(
            self.translations / "novels/12345.json",
            {"題名": "既有标题", "旧本文": "保留译文"},
        )
        master = {
            table: []
            for table in read_json(
                ROOT / "scripts/games/girlscreation/resources/master-fields.json"
            )
        }
        master["mNovels"] = [
            {"id": 12345, "title": "題名", "ml_title": ["題名", "Title"]}
        ]
        master["mItems"] = [
            {
                "id": 9,
                "ml_name": ["新道具"],
                "ml_description": ["説明{0}"],
                "ml_flavor_text": [],
            }
        ]
        write_json(self.cache / "master.json", master)
        write_json(
            self.cache / "index.json",
            {"version": 1, "novels": {"12345": {"hash": "h1"}}},
        )
        write_json(
            self.cache / "novels/12345.json",
            {
                "id": "12345",
                "messages": [
                    {"kind": "title", "name": "", "message": "題名", "line": 1},
                    {
                        "kind": "message",
                        "name": "新登場人物",
                        "message": "本文<br>{0}",
                        "line": 2,
                    },
                ],
            },
        )

    def prepare(self):
        return prepare_tasks(
            self.project, self.target, check_existing=True
        ).model_dump()

    def fill(self, plan):
        values = {
            "新登場人物": "新登场人物",
            "本文<br>{0}": "正文<br>{0}",
            "新道具": "新道具",
            "説明{0}": "说明{0}",
        }
        session = setup_session(self.work)
        for phase in (True, False):
            pending = [
                t
                for t in plan["tasks"]
                if t["term"] == phase and t["id"] not in session.answers()
            ]
            if pending:
                session.submit(
                    {
                        "translations": [
                            {
                                "id": t["id"],
                                "translation": t["reuse"] or values[t["source"]],
                            }
                            for t in pending
                        ]
                    }
                )
        session.finalize()

    def test_incremental_titles_merge_and_idempotence(self):
        plan = self.prepare()
        self.assertEqual(plan["tasks"][0]["category"], "names")
        title = next(t for t in plan["tasks"] if t["source"] == "題名")
        self.assertEqual(title["reuse"], "旧标题")
        self.assertEqual(len(title["targets"]), 1)
        self.fill(plan)
        self.assertEqual(merge_results(self.project, self.target), 4)
        self.assertEqual(merge_results(self.project, self.target), 0)
        master = read_json(self.translations / "master.json")
        self.assertEqual(master["mNovels"]["ml_title[]"]["題名"], "旧标题")
        self.assertEqual(master["mNovels"]["title"]["題名"], "旧标题")
        self.assertEqual(
            read_json(self.translations / "novels/12345.json")["旧本文"], "保留译文"
        )
        self.assertEqual(self.prepare()["tasks"], [])

    def test_manual_edit_blocks_entire_merge(self):
        plan = self.prepare()
        self.fill(plan)
        file = self.translations / "novels/12345.json"
        write_json(
            file,
            {
                "題名": "既有标题",
                "旧本文": "保留译文",
                "本文<br>{0}": "人工译文<br>{0}",
            },
        )
        before = {p: p.read_bytes() for p in self.translations.rglob("*.json")}
        with self.assertRaisesRegex(ValueError, "changed since prepare"):
            merge_results(self.project, self.target)
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.translations.rglob("*.json")}
        )

    def test_incomplete_and_traversal_results_cannot_write(self):
        plan = self.prepare()
        self.fill(plan)
        result = read_json(self.work / "results.json")
        result["translations"].pop()
        write_json(self.work / "results.json", result)
        with self.assertRaisesRegex(ValueError, "Missing"):
            merge_results(self.project, self.target)
        self.fill(plan)
        plan["tasks"][0]["targets"][0]["file"] = "../../outside.json"
        write_json(self.work / "plan.json", plan)
        with self.assertRaisesRegex(ValueError, "Invalid translation target"):
            merge_results(self.project, self.target)

    def test_names_feed_subsequent_translation_context(self):
        plan = self.prepare()
        session = setup_session(self.work)
        self.assertEqual(session.next_group()["group"], "names")
        name = next(t for t in plan["tasks"] if t["category"] == "names")
        novel = next(t for t in plan["tasks"] if t["source"] == "本文<br>{0}")
        with self.assertRaisesRegex(ValueError, "Complete and submit terminology"):
            session.submit(
                {"translations": [{"id": novel["id"], "translation": "正文<br>{0}"}]}
            )
        session.submit(
            {"translations": [{"id": name["id"], "translation": "新登场人物"}]}
        )
        group = session.next_group()
        self.assertEqual(group["group"], "novels-12345")
        self.assertEqual(
            read_json(Path(group["terms_file"]))["新登場人物"][0]["translation"],
            "新登场人物",
        )
        self.assertIn("12345", session.context(novel["id"])["stories"])

    def test_agent_repairs_invalid_draft_and_resumes(self):
        plan = self.prepare()
        session = setup_session(self.work)
        name = next(t for t in plan["tasks"] if t["category"] == "names")
        session.submit(
            {"translations": [{"id": name["id"], "translation": "新登场人物"}]}
        )
        novel = next(t for t in plan["tasks"] if t["source"] == "本文<br>{0}")
        with self.assertRaisesRegex(ValueError, "tag"):
            session.submit(
                {"translations": [{"id": novel["id"], "translation": "正文"}]}
            )
        self.assertNotIn(novel["id"], session.answers())
        session.submit(
            {"translations": [{"id": novel["id"], "translation": "正文<br>{0}"}]}
        )
        resumed = setup_session(self.work)
        self.assertEqual(resumed.status()["remaining"], 2)
        self.assertEqual(resumed.next_group()["group"], "master-mItems")
        with self.assertRaisesRegex(ValueError, "tasks remain"):
            resumed.finalize()
        remaining = [t for t in plan["tasks"] if t["id"] not in resumed.answers()]
        resumed.submit(
            {
                "translations": [
                    {"id": t["id"], "translation": t["source"]} for t in remaining
                ]
            }
        )
        self.assertEqual(Session(self.work).finalize()["remaining"], 0)

    def test_evidenced_term_proposals_are_merged_additively(self):
        plan = self.prepare()
        glossary = self.root / "glossary.json"
        write_json(glossary, read_json(ROOT / "glossary/zh-Hans.json"))
        session = setup_session(self.work)
        name = next(t for t in plan["tasks"] if t["category"] == "names")
        session.submit(
            {"translations": [{"id": name["id"], "translation": "新登场人物"}]}
        )
        proposal = {
            "source": "新登場人物",
            "translation": "新登场人物",
            "note": "剧情中的新角色",
            "evidence": name["id"],
        }
        session.propose([proposal])
        with self.assertRaisesRegex(ValueError, "Existing term conflict"):
            session.propose([{**proposal, "translation": "别的名字"}])
        with self.assertRaisesRegex(ValueError, "does not occur"):
            session.propose([{**proposal, "source": "凭空的术语"}])
        self.fill(plan)
        draft = read_json(self.work / "results.json")
        session.submit({"translations": draft["translations"]})
        session.finalize()
        merge_results(self.project, self.target)
        self.assertEqual(merge_results(self.project, self.target), 0)
        self.assertEqual(read_json(glossary)["新登場人物"]["reference"], "新登場人物")

    def test_agent_final_prose_cannot_replace_validated_results(self):
        self.prepare()
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}),
            patch("scripts.translate.execute_codex"),
        ):
            with self.assertRaisesRegex(ValueError, "tasks remain"):
                translate_plan(self.work, self.project.backend(self.target))

    def test_agent_cannot_modify_publication_inputs(self):
        self.prepare()

        def modify_input(*_args):
            write_json(self.translations / "master.json", {"changed": "unexpected"})

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}),
            patch("scripts.translate.execute_codex", side_effect=modify_input),
        ):
            with self.assertRaisesRegex(ValueError, "protected inputs"):
                translate_plan(self.work, self.project.backend(self.target))


class ValidationTests(unittest.TestCase):
    def test_markup_and_placeholders(self):
        rules = {"preserve_tags": True, "protected_patterns": [PROTECTED.pattern]}
        validate_translation(
            "値{0}<br><color=red>文</color>", "值{0}<br><color=red>文本</color>", rules
        )
        for value in ["值<br>", "值{1}<br>", "值{0}", ""]:
            with self.assertRaises(ValueError):
                validate_translation("値{0}<br>", value, rules)
        with self.assertRaisesRegex(ValueError, "tag order"):
            validate_translation("<b><i>文</i></b>", "<i><b>文本</b></i>", rules)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_results(
                [
                    Task.model_construct(
                        id="one", source="文", category="dialogue", rules={}
                    )
                ],
                {
                    "translations": [
                        {"id": "one", "translation": "文本"},
                        {"id": "one", "translation": "文本"},
                    ]
                },
            )

    def test_name_identifiers_and_master_numbers(self):
        for kind, source, value in [
            ("names", "村人Ａ＆村人Ｂ", "村民A＆村民B"),
            ("master", "攻撃力20%増加", "攻击力提高30%"),
        ]:
            with self.assertRaisesRegex(ValueError, "identifiers or master numbers"):
                validate_results(
                    [
                        Task.model_construct(
                            id="one", category=kind, source=source, rules={}
                        )
                    ],
                    {
                        "translations": [
                            {"id": "one", "translation": value},
                        ]
                    },
                    {"name_kinds": ["names"], "number_kinds": ["master"]},
                )

    def test_glossary_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "glossary.json"
            write_json(path, {"原名": {"translation": "冲突"}})
            with self.assertRaisesRegex(ValueError, "conflict"):
                resolve_glossary(path, {"原名": "标准名"})

    def test_master_character_names_are_available_before_speaking(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "glossary.json"
            write_json(path, {})
            master = {"characters": {"新人": "新角色"}}
            self.assertEqual(
                resolve_glossary(path, {}, master)["新人"][0].translation, "新角色"
            )
            with self.assertRaisesRegex(ValueError, "Master/name conflict"):
                resolve_glossary(path, {"新人": "其他译名"}, master)


if __name__ == "__main__":
    unittest.main()
