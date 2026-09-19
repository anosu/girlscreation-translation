"""Offline Girls Creation fixtures through sync, Agent sessions and publication."""

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import tomli_w

from adapters.girlscreation import collect
from adapters.girlscreation.parse import parse_script
from workflow.adapters import CollectRequest
from workflow.cli import check_translations
from workflow.config import ROOT, load_project
from workflow.merge import merge_results
from workflow.prepare import prepare_tasks
from workflow.scaffold import create_project
from workflow.session import setup_session
from workflow.snapshot import read_resource, sync_sources
from workflow.utils import read_json, write_json


class GameAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "game"
        self.config = create_project(self.root)
        settings = tomllib.loads(self.config.read_text("utf-8"))
        settings["project"]["adapter"] = "adapters.girlscreation"
        settings["adapter"] = {"input": "raw", "master_fields": "fields.json"}
        actual = load_project(ROOT / "game/translation.toml")
        settings["targets"]["zh-Hans"]["term_sources"] = [
            s.model_dump() for s in actual.targets["zh-Hans"].term_sources
        ]
        settings["targets"]["zh-Hans"]["rules"] = "rules.json"
        write_json(self.root / "rules.json", actual.targets["zh-Hans"].rules)
        self.config.write_text(tomli_w.dumps(settings), encoding="utf-8")
        self.project = load_project(self.config)
        self.target = self.project.targets["zh-Hans"]
        self.raw = self.root / "raw"
        write_json(
            self.root / "fields.json",
            {
                "mActionPatterns": {"name": "string"},
                "mActiveSkillSideEffectFilters": {"ml_name": "array"},
                "mAthenesRecordAreas": {"ml_name": "strings"},
                "mUnits": {"ml_name": "array"},
                "mNovels": {"title": "string", "ml_title": "array"},
            },
        )
        write_json(
            self.raw / "index.json",
            {
                "novels": {"12345": {}, "12346": {}},
                "fetched_novels": ["12345", "12346"],
            },
        )
        self.master = {
            "mActionPatterns": [
                {"id": 1, "name": "AI：攻撃的"},
                {"id": 2, "name": "AI：攻撃的"},
            ],
            "mActiveSkillSideEffectFilters": [
                {"id": 1, "ml_name": ["再行動", "Act again"]}
            ],
            "mAthenesRecordAreas": [{"id": 1, "ml_name": "村|Village"}],
            "mUnits": [{"id": 1, "ml_name": ["ゴッホ", "Van Gogh"]}],
            "mNovels": [{"id": 12345, "title": "題名", "ml_title": ["題名", "Title"]}],
        }
        write_json(self.raw / "master.json", self.master)
        write_json(
            self.raw / "novels/12345.json",
            {
                "messages": parse_script(
                    "title,題名,\nmessage,村人,こんにちは。,\nmsgvoicesync,chara_1,案内人,橋を渡って。<br>{0}へ。,\nmessage,,風が吹いた。,\nmessage,村人,こんにちは。,"
                )
            },
        )
        write_json(
            self.raw / "novels/12346.json",
            {"messages": parse_script("message,,夜になった。,")},
        )
        self.env = patch.dict(os.environ, {"GIRLSCREATION_CHECK_EXISTING": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, selection=None):
        return CollectRequest(
            self.root,
            self.project.options,
            self.project.sources.parent / "adapter-cache",
            selection,
            {"zh-Hans": self.target.translations},
        )

    def test_real_script_and_master_shapes_keep_source_order_and_output_keys(self):
        with patch(
            "adapters.girlscreation.fetch.fetch_sources",
            side_effect=AssertionError("offline import contacted CDN"),
        ):
            snapshot = sync_sources(self.project)
        resources = {
            key: read_resource(self.project.sources, file)
            for key, file in snapshot.resources.items()
        }
        story = resources["novels/12345"]
        self.assertEqual(
            story.lines,
            [
                ["村人", "こんにちは。"],
                ["案内人", "橋を渡って。<br>{0}へ。"],
                [None, "風が吹いた。"],
                ["村人", "こんにちは。"],
            ],
        )
        self.assertEqual(story.context, {"title": "題名"})
        self.assertEqual(resources["novels/12346"].context, {})
        self.assertNotIn("novels/12346/titles", resources)
        self.assertEqual(resources["novels/12345/titles"].output, story.output)
        self.assertEqual(resources["names"].blocks[0].texts, ["村人", "案内人"])
        fields = {
            tuple(r.path): r.blocks[0].texts
            for r in resources.values()
            if r.output == "master.json"
        }
        self.assertEqual(fields[("mActionPatterns", "name")], ["AI：攻撃的"])
        self.assertEqual(
            fields[("mActiveSkillSideEffectFilters", "ml_name[]")], ["再行動"]
        )
        self.assertEqual(fields[("mAthenesRecordAreas", "ml_name|")], ["村"])

    def test_publication_reuses_canonical_names_and_keeps_existing_title_locations(
        self,
    ):
        write_json(
            self.target.translations / "names.json", {"村人": "村民", "旧人": "旧角色"}
        )
        write_json(
            self.target.translations / "master.json",
            {
                "mNovels": {"title": {"題名": "标准标题"}},
                "mUnits": {"ml_name[]": {"ゴッホ": "梵高"}},
                "untouched": {"name": {"旧文": "旧译"}},
            },
        )
        write_json(
            self.target.translations / "novels/12345.json",
            {"旧台詞": "旧台词", "題名": "剧情旧标题"},
        )
        sync_sources(self.project, ["12345"])
        with patch(
            "adapters.girlscreation.collect",
            side_effect=AssertionError("plan called adapter"),
        ):
            plan = prepare_tasks(self.project, self.target)
        self.assertNotIn("村人", [t.source for t in plan.tasks])
        self.assertNotIn("ゴッホ", [t.source for t in plan.tasks])
        self.assertEqual(sum(t.source == "こんにちは。" for t in plan.tasks), 1)
        session = setup_session(self.target.work)
        translations = {
            "案内人": "向导",
            "題名": "标准标题",
            "こんにちは。": "你好。",
            "橋を渡って。<br>{0}へ。": "过桥。<br>前往{0}。",
            "風が吹いた。": "风吹过。",
        }
        while session.status()["remaining"]:
            packet = session.next_group()
            session.submit_packet(
                packet["packet"],
                {
                    str(i): translations.get(task.source, task.source)
                    for i, task in enumerate(session.packet_tasks(packet["packet"]), 1)
                },
            )
            session.finish_packet(packet["packet"])
        session.finalize()
        merge_results(self.project, self.target)
        novel = read_json(self.target.translations / "novels/12345.json")
        self.assertEqual(novel["題名"], "剧情旧标题")
        self.assertEqual(novel["旧台詞"], "旧台词")
        self.assertEqual(
            read_json(self.target.translations / "names.json")["案内人"], "向导"
        )
        master = read_json(self.target.translations / "master.json")
        self.assertEqual(master["mNovels"]["ml_title[]"]["題名"], "标准标题")
        self.assertEqual(master["untouched"], {"name": {"旧文": "旧译"}})
        self.assertFalse((self.target.translations / "titles.json").exists())
        check_translations(self.project, self.target)
        self.assertEqual(prepare_tasks(self.project, self.target).tasks, [])

    def test_daily_scope_explicit_ids_and_history_override(self):
        write_json(self.target.translations / "novels/12345.json", {"旧文": "旧译"})
        ids = {r.id for r in collect(self.request())}
        self.assertNotIn("novels/12345", ids)
        self.assertIn("novels/12346", ids)
        ids = {r.id for r in collect(self.request(["12345"]))}
        self.assertIn("novels/12345", ids)
        self.assertNotIn("novels/12346", ids)
        with patch.dict(os.environ, {"GIRLSCREATION_CHECK_EXISTING": "true"}):
            self.assertIn("novels/12345", {r.id for r in collect(self.request())})
        request = self.request()
        request.translations["es"] = self.root / "spanish"
        self.assertIn("novels/12345", {r.id for r in collect(request)})
        for selection in (["99999"], ["../12345"]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                list(collect(self.request(selection)))

    def test_limit_two_selects_new_story_when_names_are_already_translated(self):
        write_json(
            self.target.translations / "names.json", {"村人": "村民", "案内人": "向导"}
        )
        write_json(
            self.target.translations / "novels/12346.json", {"夜になった。": "天黑了。"}
        )
        sync_sources(self.project)
        plan = prepare_tasks(self.project, self.target, limit=2)
        self.assertEqual({task.output for task in plan.tasks}, {"novels/12345.json"})
        self.assertEqual(len(plan.tasks), 4)
        self.assertTrue({"novels/12345", "novels/12345/titles"} <= set(plan.resources))
        report = read_json(self.target.work / "prepare-report.json")
        self.assertEqual(
            (report["tasks"], report["available_tasks"], report["deferred_tasks"]),
            (4, 10, 6),
        )

    def test_failed_extraction_retains_last_immutable_snapshot(self):
        sync_sources(self.project)
        pointer = (self.project.sources / "index.json").read_bytes()
        del self.master["mActionPatterns"][0]["name"]
        write_json(self.raw / "master.json", self.master)
        with self.assertRaisesRegex(ValueError, "Missing master field"):
            sync_sources(self.project)
        self.assertEqual((self.project.sources / "index.json").read_bytes(), pointer)
        self.assertTrue(prepare_tasks(self.project, self.target).tasks)

    def test_live_collection_forwards_scope_to_the_game_fetcher(self):
        request = CollectRequest(
            self.root,
            {"master_fields": "fields.json", "check_existing": True},
            self.raw,
            ["12345"],
            {"zh-Hans": self.target.translations},
        )
        with (
            patch.dict(os.environ, {"GIRLSCREATION_CHECK_EXISTING": ""}),
            patch(
                "adapters.girlscreation.fetch.fetch_sources",
                return_value=read_json(self.raw / "index.json"),
            ) as fetch,
        ):
            list(collect(request))
        fetch.assert_called_once_with(
            self.raw,
            ["12345"],
            translations=[self.target.translations],
            check_existing=True,
        )

    def test_title_and_role_with_the_same_source_are_not_merged_as_global_terms(self):
        write_json(
            self.target.translations / "names.json",
            {"自画像のイマージュ": "自画像形象"},
        )
        write_json(
            self.target.translations / "master.json",
            {"mNovels": {"title": {"自画像のイマージュ": "自画像的形象"}}},
        )
        sync_sources(self.project)
        prepare_tasks(self.project, self.target)
        session = setup_session(self.target.work)
        self.assertEqual(
            session.projected_names({})["自画像のイマージュ"], "自画像形象"
        )


if __name__ == "__main__":
    unittest.main()
