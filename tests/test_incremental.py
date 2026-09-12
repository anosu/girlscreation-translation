"""Publication-driven updates and optional checks of existing novels."""

import json
import unittest
import zipfile
from dataclasses import replace
from unittest.mock import patch

import test_framework
import test_pipeline

from scripts.merge import merge_results
from scripts.prepare import compile_catalog, prepare_tasks, source_catalog
from scripts.session import setup_session
from scripts.utils import read_json, write_json


class IncrementalTests(unittest.TestCase):
    setUp = test_framework.FrameworkTests.setUp
    translate = test_framework.FrameworkTests.translate

    def test_existing_title_variants_do_not_block_normal_updates(self):
        entries = [
            {
                "id": "title",
                "category": "novels",
                "group": "titles",
                "source": "難しいテーマ",
                "context": {},
                "reconcile": True,
                "targets": [
                    {
                        "file": name,
                        "path": ["難しいテーマ"],
                        "priority": 100,
                    }
                    for name in ("one.json", "two.json")
                ],
            }
        ]
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        target = self.project.targets["es"]
        write_json(target.translations / "one.json", {"難しいテーマ": "困难的主题"})
        write_json(target.translations / "two.json", {"難しいテーマ": "复杂的主题"})
        plan = prepare_tasks(self.project, target)
        self.assertEqual(plan.tasks, [])
        plan = prepare_tasks(self.project, target, check_existing=True)
        self.assertEqual(plan.tasks, [])
        report = read_json(target.work / "prepare-report.json")
        self.assertEqual(len(report["existing_variants"]), 1)
        self.assertEqual(
            read_json(target.translations / "one.json")["難しいテーマ"], "困难的主题"
        )

    def test_completed_outputs_generate_no_tasks_or_publication_changes(self):
        target = self.project.targets["es"]
        prepare_tasks(self.project, target)
        self.translate("es")
        merge_results(self.project, target)
        catalog = source_catalog(self.project, targets=[target])
        plan = prepare_tasks(self.project, target, catalog)
        self.assertEqual(plan.tasks, [])
        setup_session(target.work).finalize()
        with patch(
            "scripts.merge.make_manifest",
            side_effect=AssertionError("No publication change"),
        ):
            self.assertEqual(merge_results(self.project, target), 0)

    def test_limit_preserves_unfinished_entries_without_resource_records(self):
        target = self.project.targets["es"]
        prepare_tasks(self.project, target, limit=1)
        self.translate("es")
        merge_results(self.project, target)
        self.assertFalse((self.root / "translation-state").exists())
        plan = prepare_tasks(self.project, target)
        self.assertEqual(len(plan.tasks), 3)
        self.translate("es")
        merge_results(self.project, target)
        source = read_json(self.root / "source.json")
        source[-1]["source"] = "Continue game"
        source[-1]["targets"][0]["path"] = ["menu", "continue"]
        write_json(self.root / "source.json", source)
        self.adapter.fetch(self.project.sources, None, self.options)
        self.assertEqual(len(prepare_tasks(self.project, target).tasks), 1)

    def test_complete_check_is_explicit(self):
        target = self.project.targets["es"]
        prepare_tasks(self.project, target)
        self.translate("es")
        merge_results(self.project, target)
        with patch.object(
            self.adapter, "extract", wraps=self.adapter.extract
        ) as extract:
            compile_catalog(self.project, targets=[target], check_existing=True)
        self.assertEqual(extract.call_count, 1)

    def test_limited_audit_keeps_historical_gaps_pending(self):
        target = self.project.targets["es"]
        prepare_tasks(self.project, target)
        self.translate("es")
        merge_results(self.project, target)
        write_json(target.translations / "characters.json", {"retired": "Keep"})
        write_json(
            target.translations / "interface.json", {"menu": {"retired": "Keep"}}
        )
        prepare_tasks(self.project, target, check_existing=True, limit=1)
        self.translate("es")
        merge_results(self.project, target)
        plan = prepare_tasks(self.project, target)
        self.assertEqual([t.source for t in plan.tasks], ["Start game"])

    def test_languages_compare_their_own_publications(self):
        spanish, chinese = (self.project.targets[code] for code in ("es", "zh-Hans"))
        prepare_tasks(self.project, spanish)
        self.translate("es")
        merge_results(self.project, spanish)
        catalog = source_catalog(self.project)
        self.assertEqual(len(prepare_tasks(self.project, chinese, catalog).tasks), 4)
        self.assertEqual(prepare_tasks(self.project, spanish, catalog).tasks, [])
        self.assertFalse(chinese.translations.exists())

    def test_exported_snapshot_preserves_integrity_without_other_cache_files(self):
        target = self.project.targets["es"]
        catalog = compile_catalog(self.project, targets=[target], export=True)
        copied = replace(self.project, sources=self.root / "downloaded")
        with zipfile.ZipFile(self.project.source_bundle) as archive:
            archive.extractall(copied.sources)
        write_json(copied.sources / "unrelated.json", {"cache": "unused"})
        loaded = source_catalog(copied, self.project.catalog, targets=[target])
        self.assertEqual(loaded.source_version, catalog.source_version)
        prepare_tasks(copied, target, loaded)
        setup_session(target.work)
        reference = loaded.entries[0].references[0]
        write_json(copied.sources / reference, [])
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            setup_session(target.work)
        with self.assertRaisesRegex(ValueError, "catalog is stale"):
            source_catalog(copied, self.project.catalog, targets=[target])

    def test_catalog_inputs_survive_checkout_line_endings_and_json_formatting(self):
        module = self.root / "adapter.py"
        module.write_bytes(b"# Adapter implementation\n")
        with patch.object(self.adapter, "__file__", str(module)):
            compile_catalog(self.project)
            module.write_bytes(b"# Adapter implementation\r\n")
            source = self.root / "source.json"
            source.write_text(json.dumps(read_json(source)), encoding="utf-8")
            with patch.object(self.adapter, "extract") as extract:
                catalog = source_catalog(self.project, self.project.catalog)
            extract.assert_not_called()
            self.assertEqual(len(catalog.entries), 4)

    def test_ambiguous_missing_title_remains_pending_without_overwriting(self):
        entries = [
            {
                "id": "title",
                "category": "novels",
                "group": "titles",
                "source": "難しいテーマ",
                "context": {},
                "reconcile": True,
                "targets": [
                    {"file": name, "path": ["難しいテーマ"]}
                    for name in ("one.json", "two.json", "three.json")
                ],
            }
        ]
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        target = self.project.targets["es"]
        write_json(target.translations / "one.json", {"難しいテーマ": "困难的主题"})
        write_json(target.translations / "two.json", {"難しいテーマ": "复杂的主题"})
        plan = prepare_tasks(self.project, target)
        self.assertEqual(plan.tasks, [])
        report = read_json(target.work / "prepare-report.json")
        self.assertEqual(report["existing_variants"][0]["missing_outputs"], 1)
        setup_session(target.work).finalize()
        merge_results(self.project, target)
        self.assertFalse((target.translations / "three.json").exists())


class GirlsIncrementalTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    fill = test_pipeline.PipelineTests.fill

    def finish(self, check_existing=False):
        plan = prepare_tasks(self.project, self.target, check_existing=check_existing)
        self.fill(plan.model_dump())
        merge_results(self.project, self.target)

    def test_daily_checks_master_and_audit_finds_existing_story_gaps(self):
        plan = prepare_tasks(self.project, self.target)
        self.assertEqual({t.source for t in plan.tasks}, {"題名", "新道具", "説明{0}"})
        self.finish()
        self.assertFalse((self.root / "translation-state").exists())
        plan = prepare_tasks(self.project, self.target, check_existing=True)
        self.assertEqual({t.source for t in plan.tasks}, {"新登場人物", "本文<br>{0}"})
        self.assertEqual(
            len(read_json(self.work / "prepare-report.json")["existing_variants"]), 1
        )

    def test_master_is_compared_every_run_without_a_version_change(self):
        self.finish()
        self.assertEqual(prepare_tasks(self.project, self.target).tasks, [])
        master = read_json(self.cache / "master.json")
        master["mItems"][0]["ml_name"] = ["更新道具"]
        write_json(self.cache / "master.json", master)
        plan = prepare_tasks(self.project, self.target)
        self.assertEqual([t.source for t in plan.tasks], ["更新道具"])

    def test_existing_novel_changes_are_only_checked_explicitly(self):
        self.finish(check_existing=True)
        novel = read_json(self.cache / "novels/12345.json")
        novel["messages"][-1]["message"] = "更新本文"
        write_json(self.cache / "novels/12345.json", novel)
        index = read_json(self.cache / "index.json")
        index["novels"]["12345"]["hash"] = "new-version"
        write_json(self.cache / "index.json", index)
        with patch(
            "scripts.games.girlscreation.adapter.read_json", wraps=read_json
        ) as reads:
            plan = prepare_tasks(self.project, self.target)
        self.assertEqual(plan.tasks, [])
        self.assertNotIn(
            self.cache / "novels/12345.json",
            [call.args[0] for call in reads.call_args_list],
        )
        self.assertNotIn(
            self.translations / "novels/12345.json",
            [call.args[0] for call in reads.call_args_list],
        )
        plan = prepare_tasks(self.project, self.target, check_existing=True)
        self.assertEqual([t.source for t in plan.tasks], ["更新本文"])

    def test_missing_novel_supplies_names_without_reading_existing_novels(self):
        index = read_json(self.cache / "index.json")
        index["novels"]["67890"] = {"hash": "h2"}
        index["fetched_novels"] = ["12345"]
        write_json(self.cache / "index.json", index)
        write_json(
            self.translations / "novels/67890.json", {"unchanged source": "旧译文"}
        )
        (self.translations / "novels/12345.json").unlink()
        with patch(
            "scripts.games.girlscreation.adapter.read_json", wraps=read_json
        ) as reads:
            catalog = source_catalog(self.project, targets=[self.target])
        self.assertNotIn(
            self.cache / "novels/67890.json",
            [call.args[0] for call in reads.call_args_list],
        )
        plan = prepare_tasks(self.project, self.target, catalog)
        self.assertIn("新登場人物", [t.source for t in plan.tasks])
        self.assertIn("本文<br>{0}", [t.source for t in plan.tasks])
        self.assertNotIn("novels/67890.json", catalog.source_files)
        with self.assertRaisesRegex(ValueError, "rerun fetch"):
            source_catalog(self.project, targets=[self.target], check_existing=True)

    def test_limit_cannot_publish_half_a_new_novel(self):
        self.finish(check_existing=True)
        novel = self.translations / "novels/12345.json"
        novel.unlink()
        with self.assertRaisesRegex(ValueError, "complete new file"):
            prepare_tasks(self.project, self.target, limit=1)
        self.assertFalse(novel.exists())
        plan = prepare_tasks(self.project, self.target, limit=2)
        self.assertEqual(len(plan.tasks), 2)
        self.fill(plan.model_dump())
        merge_results(self.project, self.target)
        self.assertEqual(set(read_json(novel)), {"題名", "本文<br>{0}"})
        self.assertEqual(read_json(novel)["題名"], "旧标题")
        self.assertEqual(prepare_tasks(self.project, self.target).tasks, [])
