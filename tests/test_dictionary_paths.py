"""Nested dictionaries and canonical names through the public resource workflow."""

import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path

import tomli_w

from workflow.cli import check_translations
from workflow.config import TermSource, load_project
from workflow.merge import merge_results
from workflow.operations import run_summary
from workflow.prepare import prepare_tasks
from workflow.resources import Resource
from workflow.scaffold import create_project
from workflow.session import Session, setup_session
from workflow.snapshot import sync_sources
from workflow.utils import read_json, write_json


def text_resource(id, output, path, texts, **options):
    return {
        "id": id,
        "output": output,
        "path": path,
        "kind": "text",
        "blocks": [{"texts": texts}],
        **options,
    }


class DictionaryPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "game"
        self.config = create_project(self.root)
        config = tomllib.loads(self.config.read_text(encoding="utf-8"))
        config["targets"]["zh-Hans"]["term_sources"] = [{"file": "names.json"}]
        self.config.write_text(tomli_w.dumps(config), encoding="utf-8")
        self.project = load_project(self.config)
        self.target = self.project.targets["zh-Hans"]
        self.resources = [
            text_resource("names", "names.json", [], ["村人", "主人公"]),
            {
                "id": "story",
                "output": "novels/1.json",
                "kind": "dialogue",
                "lines": [["村人", "ははは"], [None, "ははは2"], ["主人公", "ははは3"]],
                "context": {"title": "握手は心の中だけで"},
            },
            text_resource(
                "actions",
                "master.json",
                ["mActionPatterns", "name"],
                ["AI：攻撃的", "AI：防御的"],
            ),
            text_resource(
                "filters",
                "master.json",
                ["mActiveSkillSideEffectFilters", "ml_name[]"],
                ["即死", "再行動"],
            ),
            text_resource("titles", "titles.json", [], ["握手は心の中だけで"]),
        ]
        self.values = {
            "村人": "村民",
            "主人公": "主人公",
            "ははは": "哈哈哈",
            "ははは2": "哈哈哈2",
            "ははは3": "哈哈哈3",
            "AI：攻撃的": "AI：攻击型",
            "AI：防御的": "AI：防御型",
            "即死": "即死",
            "再行動": "再行动",
            "握手は心の中だけで": "只在心中握手",
        }

    def plan(self, resources=None, limit=None):
        write_json(
            self.root / "sources/resources.json",
            self.resources if resources is None else resources,
        )
        sync_sources(self.project)
        return prepare_tasks(self.project, self.target, limit)

    def finish(self, values=None):
        session = setup_session(self.target.work)
        while session.status()["remaining"]:
            packet = session.next_group()
            payload = {}
            for i, task in enumerate(session.packet_tasks(packet["packet"]), 1):
                key = (task.output, (*task.path, task.source))
                payload[str(i)] = (values or {}).get(
                    key, self.values.get(task.source, task.source)
                )
            session.submit_packet(packet["packet"], payload)
            session.finish_packet(packet["packet"])
        session.finalize()
        return session

    def test_names_multiple_tables_and_titles_publish_together(self):
        old = {
            "mActionPatterns": {
                "name": {"AI：攻撃的": "已有攻击型", "旧指令": "保留"},
                "description": {"旧描述": "保留描述"},
            },
            "unselected": {"field": {"历史": "保留历史"}},
        }
        write_json(self.target.translations / "master.json", old)
        write_json(
            self.target.translations / "names.json", {"村人": "村民", "旧人": "旧角色"}
        )
        plan = self.plan()
        self.assertNotIn("村人", [task.source for task in plan.tasks])
        session = setup_session(self.target.work)
        first = session.next_group()
        self.assertEqual(first["resource"], "names")
        self.assertEqual(first["pending"], ["1"])
        session.submit_packet(first["packet"], {"1": "主人公"})
        story = session.next_group()
        self.assertEqual(story["resource"], "story")
        self.assertEqual(story["page"]["terms"]["村人"][0]["translation"], "村民")
        self.assertEqual(story["page"]["terms"]["主人公"][0]["translation"], "主人公")
        self.finish()
        merge_results(self.project, self.target)
        result = read_json(self.target.translations / "master.json")
        self.assertEqual(
            result["mActionPatterns"]["name"],
            {"AI：攻撃的": "已有攻击型", "旧指令": "保留", "AI：防御的": "AI：防御型"},
        )
        self.assertEqual(
            result["mActiveSkillSideEffectFilters"]["ml_name[]"],
            {"即死": "即死", "再行動": "再行动"},
        )
        self.assertEqual(result["unselected"], old["unselected"])
        self.assertEqual(
            result["mActionPatterns"]["description"],
            old["mActionPatterns"]["description"],
        )
        self.assertEqual(
            read_json(self.target.translations / "names.json"),
            {"村人": "村民", "旧人": "旧角色", "主人公": "主人公"},
        )
        self.assertEqual(read_json(self.target.glossary), {})
        self.assertEqual(
            read_json(self.target.translations / "titles.json"),
            {"握手は心の中だけで": "只在心中握手"},
        )
        check_translations(self.project, self.target)
        self.assertEqual(merge_results(self.project, self.target), 0)
        self.assertEqual(prepare_tasks(self.project, self.target).tasks, [])

    def test_same_source_in_different_tables_has_independent_packets_and_values(self):
        resources = [
            text_resource("one", "master.json", ["table1", "name"], ["共通"]),
            text_resource("two", "master.json", ["table2", "ml_name[]"], ["共通"]),
        ]
        plan = self.plan(resources)
        self.assertEqual(len(plan.tasks), 2)
        self.assertNotEqual(plan.tasks[0].id, plan.tasks[1].id)
        session = setup_session(self.target.work)
        first = session.next_group()
        session.submit_packet(first["packet"], {"1": "译法甲"})
        second = session.next_group()
        self.assertEqual(second["pending"], ["1"])
        self.assertNotIn("译法甲", second["page"]["text"])
        session.submit_packet(second["packet"], {"1": "译法乙"})
        session.finalize()
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "master.json"),
            {
                "table1": {"name": {"共通": "译法甲"}},
                "table2": {"ml_name[]": {"共通": "译法乙"}},
            },
        )

    def test_dictionary_path_keys_and_source_are_not_normalized(self):
        source = "  原文\n"
        self.plan(
            [
                text_resource(
                    "literal", "master.json", ["table/name", "ml_name[]"], [source]
                )
            ]
        )
        self.finish({("master.json", ("table/name", "ml_name[]", source)): "译文"})
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "master.json"),
            {"table/name": {"ml_name[]": {source: "译文"}}},
        )

    def test_partial_table_sync_preserves_other_tables_and_changes_only_missing_keys(
        self,
    ):
        write_json(
            self.target.translations / "master.json",
            {"a": {"name": {"旧": "旧译"}}, "b": {"name": {"别": "另一译法"}}},
        )
        self.plan([text_resource("one", "master.json", ["a", "name"], ["新"])])
        self.finish()
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "master.json"),
            {
                "a": {"name": {"旧": "旧译", "新": "新"}},
                "b": {"name": {"别": "另一译法"}},
            },
        )

    def test_canonical_names_remain_available_without_name_resources(self):
        write_json(self.target.translations / "names.json", {"村人": "村民"})
        self.plan([self.resources[1]])
        session = setup_session(self.target.work)
        self.assertEqual(
            session.next_group()["page"]["terms"]["村人"][0]["translation"], "村民"
        )
        write_json(self.target.translations / "names.json", {"村人": "村里的居民"})
        with self.assertRaisesRegex(ValueError, "terminology changed"):
            Session(self.target.work)
        prepare_tasks(self.project, self.target)
        resumed = setup_session(self.target.work)
        self.assertEqual(
            resumed.next_group()["page"]["terms"]["村人"][0]["translation"],
            "村里的居民",
        )
        self.assertEqual(read_json(self.target.glossary), {})

    def test_canonical_annotations_follow_names_without_copying_values(self):
        write_json(self.target.translations / "names.json", {"村人": "村民"})
        write_json(self.target.glossary, {"村人": {"note": "普通角色称呼"}})
        self.plan([self.resources[1]])
        session = setup_session(self.target.work)
        term = session.next_group()["page"]["terms"]["村人"][0]
        self.assertEqual((term["translation"], term["note"]), ("村民", "普通角色称呼"))
        self.finish()
        merge_results(self.project, self.target)
        check_translations(self.project, self.target)
        self.assertNotIn("translation", read_json(self.target.glossary)["村人"])

    def test_proposals_do_not_duplicate_canonical_name_values(self):
        self.plan([self.resources[0]])
        session = setup_session(self.target.work)
        packet = session.next_group()["packet"]
        session.submit_packet(packet, {"1": "村民", "2": "主人公"})
        session.propose_packet(
            packet,
            [
                {
                    "source": "村人",
                    "translation": "村民",
                    "note": "角色称呼",
                    "evidence": "1",
                }
            ],
        )
        session.finalize()
        merge_results(self.project, self.target)
        self.assertEqual(read_json(self.target.glossary), {})

    def test_nested_canonical_source_is_supported(self):
        self.target = replace(
            self.target,
            term_sources=[TermSource(file="terms.json", path=["cast", "name"])],
        )
        write_json(
            self.target.translations / "terms.json",
            {"cast": {"name": {"村人": "村民"}}},
        )
        self.plan([self.resources[1]])
        session = setup_session(self.target.work)
        self.assertEqual(
            session.next_group()["page"]["terms"]["村人"][0]["translation"], "村民"
        )

    def test_conflicting_canonical_files_and_legacy_duplicate_glossary_fail(self):
        self.target = replace(
            self.target,
            term_sources=[TermSource(file="names.json"), TermSource(file="other.json")],
        )
        write_json(self.target.translations / "names.json", {"村人": "村民"})
        write_json(self.target.translations / "other.json", {"村人": "其他译名"})
        with self.assertRaisesRegex(ValueError, "Conflicting canonical"):
            self.plan([self.resources[1]])
        self.target = replace(self.target, term_sources=[TermSource(file="names.json")])
        write_json(self.target.glossary, {"村人": {"translation": "旧译名"}})
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.plan([self.resources[1]])

    def test_canonical_edit_after_finalize_blocks_publish(self):
        write_json(self.target.translations / "names.json", {"村人": "村民"})
        self.plan([self.resources[1]])
        self.finish()
        write_json(self.target.translations / "names.json", {"村人": "人工新译"})
        with self.assertRaisesRegex(ValueError, "terminology changed"):
            merge_results(self.project, self.target)
        self.assertFalse((self.target.translations / "novels/1.json").exists())
        with self.assertRaisesRegex(ValueError, "policy changed"):
            merge_results(self.project, replace(self.target, term_sources=[]))

    def test_existing_nonobject_parent_is_never_overwritten(self):
        old = {"table": "已有原文的译文"}
        write_json(self.target.translations / "master.json", old)
        with self.assertRaisesRegex(ValueError, "path|dictionary"):
            self.plan([text_resource("one", "master.json", ["table", "name"], ["新"])])
        self.assertEqual(read_json(self.target.translations / "master.json"), old)

    def test_overlapping_dictionary_namespaces_are_rejected_before_planning(self):
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.plan(
                [
                    text_resource("one", "master.json", ["table"], ["a"]),
                    text_resource("two", "master.json", ["table", "name"], ["b"]),
                ]
            )
        self.assertFalse((self.target.work / "plan.json").exists())

    def test_shared_file_is_not_split_by_resource_limit(self):
        with self.assertRaisesRegex(ValueError, "complete output"):
            self.plan(self.resources[2:4], limit=1)

    def test_limit_counts_pending_resources_and_replanning_advances(self):
        write_json(self.target.translations / "names.json", {"村人": "村民"})
        write_json(
            self.target.translations / "master.json", {"old": {"name": {"旧": "旧译"}}}
        )
        resources = [
            text_resource("names", "names.json", [], ["村人"]),
            text_resource("old", "master.json", ["old", "name"], ["旧"]),
            text_resource("new", "master.json", ["new", "name"], ["新"]),
            text_resource("later", "titles.json", [], ["次回"]),
        ]
        plan = self.plan(resources, limit=1)
        self.assertEqual([task.source for task in plan.tasks], ["新"])
        self.assertEqual(set(plan.resources), {"names", "old", "new"})
        session = setup_session(self.target.work)
        self.assertEqual(session.projected_names({})["村人"], "村民")
        report = read_json(self.target.work / "prepare-report.json")
        self.assertEqual(
            (report["tasks"], report["available_tasks"], report["deferred_tasks"]),
            (1, 2, 1),
        )
        self.assertEqual(
            (report["pending_resources"], report["available_pending_resources"]), (1, 2)
        )
        summary = run_summary(self.project, [self.target])
        self.assertIn("Deferred by limit", summary)
        self.assertIn("1 / 2", summary)
        self.finish()
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "master.json")["old"],
            {"name": {"旧": "旧译"}},
        )
        next_plan = prepare_tasks(self.project, self.target, limit=1)
        self.assertEqual([task.source for task in next_plan.tasks], ["次回"])

    def test_limit_reports_insufficient_capacity_after_completed_resources(self):
        write_json(
            self.target.translations / "names.json",
            {"村人": "村民", "主人公": "主人公"},
        )
        self.plan([self.resources[0]])
        previous = (self.target.work / "plan.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "use at least 2"):
            self.plan([self.resources[0], *self.resources[2:4]], limit=1)
        self.assertEqual((self.target.work / "plan.json").read_bytes(), previous)

    def test_limit_does_not_reject_a_fully_translated_shared_file(self):
        write_json(
            self.target.translations / "master.json",
            {
                "mActionPatterns": {
                    "name": {"AI：攻撃的": "AI：攻击型", "AI：防御的": "AI：防御型"}
                },
                "mActiveSkillSideEffectFilters": {
                    "ml_name[]": {"即死": "即死", "再行動": "再行动"}
                },
            },
        )
        plan = self.plan(self.resources[2:4], limit=1)
        self.assertEqual(plan.tasks, [])
        self.assertEqual(set(plan.resources), {"actions", "filters"})
        report = read_json(self.target.work / "prepare-report.json")
        self.assertEqual(
            (report["available_tasks"], report["pending_resources"]), (0, 0)
        )

    def test_repeated_nested_source_shares_one_task_and_all_occurrences(self):
        first = text_resource("one", "master.json", ["table", "name"], ["同文"])
        second = {**first, "id": "two"}
        plan = self.plan([first, second])
        self.assertEqual(len(plan.tasks), 1)
        packet = setup_session(self.target.work).next_group()
        self.assertEqual(set(packet["related_resources"]), {"one", "two"})

    def test_invalid_path_types_and_term_source_files_are_rejected(self):
        for path in ([1], [""], [" "]):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Resource.model_validate(
                    text_resource("one", "master.json", path, ["a"])
                )
        for file in ("../names.json", "/names.json", "manifest.json"):
            with self.subTest(file=file), self.assertRaises(ValueError):
                TermSource(file=file)

    def test_new_canonical_policy_does_not_reuse_an_old_plain_text_draft(self):
        self.target = replace(self.target, term_sources=[])
        self.plan([self.resources[0]])
        previous = self.finish()
        self.assertEqual(previous.status()["completed"], 2)
        self.target = replace(self.target, term_sources=[TermSource(file="names.json")])
        prepare_tasks(self.project, self.target)
        self.assertEqual(setup_session(self.target.work).status()["completed"], 0)

    def test_nested_publication_can_resume_after_one_file_was_written(self):
        self.plan()
        self.finish()
        from workflow.merge import prepare_update

        update = prepare_update(self.project, self.target)
        path = self.target.translations / "master.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(update.files[path])
        merge_results(self.project, self.target)
        self.assertEqual(merge_results(self.project, self.target), 0)
        check_translations(self.project, self.target)
