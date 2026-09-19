"""End-to-end resource contracts, packet recovery, and safe dictionary publication."""

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from workflow.adapters import load_adapter
from workflow.ci import restore_artifacts
from workflow.cli import argument_parser, check_translations, main
from workflow.config import load_project
from workflow.merge import apply_updates, merge_results, prepare_update
from workflow.operations import prune_cache, status
from workflow.prepare import prepare_tasks, read_plan
from workflow.resources import Resource
from workflow.scaffold import create_project
from workflow.session import Session, setup_session
from workflow.snapshot import read_snapshot, sync_sources
from workflow.translate import translate_plan
from workflow.utils import read_json, write_json

MATERIALS = [
    {
        "id": "cast",
        "output": "names.json",
        "kind": "text",
        "term": True,
        "blocks": [{"texts": ["アリス"]}],
    },
    {
        "id": "scene",
        "output": "novels/scene.json",
        "kind": "dialogue",
        "lines": [
            [None, "ここはどこ？"],
            ["アリス", "ようこそ、{player}！"],
            ["アリス", "ようこそ、{player}！"],
        ],
        "rules": {"protected_patterns": [r"\{[^{}]+\}"]},
    },
    {
        "id": "ui",
        "output": "ui.json",
        "kind": "text",
        "blocks": [
            {"texts": ["始める"]},
            {"context": {"path": ["任意", "項目"]}, "texts": ["続ける"]},
        ],
    },
]
VALUES = {
    "アリス": "爱丽丝",
    "ここはどこ？": "这是哪里？",
    "ようこそ、{player}！": "欢迎，{player}！",
    "始める": "开始",
    "続ける": "继续",
}


class ResourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "game"
        self.config = create_project(self.root, targets=["zh-Hans", "es"])
        self.project = load_project(self.config)
        self.target = self.project.targets["zh-Hans"]
        self.input = self.root / "sources/resources.json"
        write_json(self.input, MATERIALS)
        sync_sources(self.project)

    def plan(self, target=None, limit=None):
        return prepare_tasks(self.project, target or self.target, limit)

    def finish(self, target=None):
        target = target or self.target
        session = setup_session(target.work)
        while session.status()["remaining"]:
            packet = session.next_group()
            tasks = session.packet_tasks(packet["packet"])
            session.submit_packet(
                packet["packet"],
                {
                    str(i): VALUES.get(task.source, task.source)
                    for i, task in enumerate(tasks, 1)
                    if task.id not in session.answers()
                },
            )
            session.finish_packet(packet["packet"])
        session.finalize()
        return session

    def cli(self, *args):
        with patch("sys.argv", ["workflow", *args, "--config", str(self.config)]):
            main()

    def test_multilingual_delivery_is_exact_flat_source_dictionary(self):
        for target in self.project.targets.values():
            plan = self.plan(target)
            self.assertEqual(len(plan.tasks), 5)
            self.finish(target)
            merge_results(self.project, target)
            story = read_json(target.translations / "novels/scene.json")
            self.assertEqual(
                story,
                {key: VALUES[key] for key in ("ここはどこ？", "ようこそ、{player}！")},
            )
            self.assertEqual(
                read_json(target.translations / "names.json")["アリス"], "爱丽丝"
            )
            self.assertEqual(read_json(target.glossary), {})
            check_translations(self.project, target)
            self.assertEqual(self.plan(target).tasks, [])

    def test_plan_never_loads_adapter_or_calls_model(self):
        with (
            patch(
                "workflow.snapshot.load_adapter",
                side_effect=AssertionError("network boundary"),
            ),
            patch("workflow.cli.translate_plan") as model,
        ):
            self.cli("plan")
        model.assert_not_called()
        self.assertFalse((self.target.translations / "ui.json").exists())

    def test_failed_collection_keeps_last_snapshot_usable(self):
        index = (self.project.sources / "index.json").read_bytes()

        def failed(_request):
            yield Resource.model_validate(MATERIALS[0])
            raise OSError("download interrupted")

        with patch.object(load_adapter("json"), "collect", side_effect=failed):
            with self.assertRaises(OSError):
                sync_sources(self.project)
        self.assertEqual((self.project.sources / "index.json").read_bytes(), index)
        self.assertEqual(len(self.plan().tasks), 5)

    def test_new_sync_does_not_mutate_running_plan(self):
        self.plan()
        session = setup_session(self.target.work)
        before = session.plan.source_version
        write_json(self.input, [MATERIALS[2]])
        sync_sources(self.project)
        self.assertEqual(read_plan(self.target.work).source_version, before)
        self.finish()

    def test_partial_selection_preserves_old_dictionary(self):
        write_json(self.target.translations / "history.json", {"旧い": "旧译文"})
        sync_sources(self.project, ["ui"])
        self.assertEqual(len(self.plan().tasks), 2)
        self.finish()
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "history.json"), {"旧い": "旧译文"}
        )

    def test_unknown_selection_and_duplicate_resources_fail(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            sync_sources(self.project, ["missing"])
        write_json(self.input, [MATERIALS[0], MATERIALS[0]])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            sync_sources(self.project)

    def test_repetition_preserved_in_reading_but_not_queue(self):
        self.plan()
        session = setup_session(self.target.work)
        cast = session.next_group()
        session.submit_packet(cast["packet"], {"1": "爱丽丝"})
        packet = session.next_group()
        text = packet["page"]["text"]
        self.assertEqual(text.count("ようこそ、{player}！"), 2)
        self.assertEqual(len(packet["pending"]), 2)
        self.assertNotIn("targets", text)
        self.assertNotIn("context_version", text)
        self.assertIn("爱丽丝", json.dumps(packet["page"]["terms"], ensure_ascii=False))

    def test_packet_numbers_cannot_cross_snapshots_or_include_unknown_ids(self):
        self.plan()
        session = setup_session(self.target.work)
        packet = session.next_group()
        with self.assertRaisesRegex(ValueError, "Unknown"):
            session.submit_packet(packet["packet"], {"999": "invalid"})
        write_json(self.input, [MATERIALS[2]])
        sync_sources(self.project)
        self.plan()
        revised = setup_session(self.target.work)
        with self.assertRaisesRegex(ValueError, "stale"):
            revised.submit_packet(packet["packet"], {"1": "invalid"})

    def test_long_line_is_pageable_without_losing_characters(self):
        raw = "長い" * 20000
        write_json(
            self.input,
            [
                {
                    "id": "long",
                    "output": "long.json",
                    "kind": "dialogue",
                    "lines": [[None, raw]],
                }
            ],
        )
        sync_sources(self.project)
        self.plan()
        session = setup_session(self.target.work)
        packet = session.next_group()
        chunks, offset = [], 0
        while True:
            page = session.read_resource(
                "long", packet=packet["packet"], offset=offset, limit=1000
            )
            self.assertLessEqual(len(page["text"]), 1000)
            chunks.append(page["text"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertIn(raw, "".join(chunks))

    def test_repeated_source_in_shared_output_keeps_all_contexts(self):
        resources = [MATERIALS[1], {**MATERIALS[1], "id": "another-scene"}]
        write_json(self.input, resources)
        sync_sources(self.project)
        with self.assertRaisesRegex(ValueError, "complete output"):
            self.plan(limit=1)
        plan = self.plan(limit=2)
        self.assertEqual(len(plan.tasks), 2)
        session = setup_session(self.target.work)
        packet = session.next_group()
        self.assertEqual(set(packet["related_resources"]), {"scene", "another-scene"})
        self.assertEqual(len(session.search("ここは")["matches"]), 2)

    def test_same_source_in_different_outputs_is_independent(self):
        write_json(
            self.input,
            [
                {
                    "id": key,
                    "kind": "text",
                    "output": f"{key}.json",
                    "blocks": [{"texts": ["同文"]}],
                }
                for key in ("a", "b")
            ],
        )
        sync_sources(self.project)
        plan = self.plan()
        self.assertEqual(len(plan.tasks), 2)
        self.assertNotEqual(plan.tasks[0].id, plan.tasks[1].id)

    def test_related_resource_reads_accepted_shared_source_as_context(self):
        write_json(
            self.input,
            [
                {
                    "id": "first",
                    "output": "shared.json",
                    "kind": "text",
                    "blocks": [{"texts": ["共通"]}],
                },
                {
                    "id": "second",
                    "output": "shared.json",
                    "kind": "text",
                    "blocks": [{"texts": ["共通", "次"]}],
                },
            ],
        )
        sync_sources(self.project)
        self.plan()
        session = setup_session(self.target.work)
        first = session.next_group()
        session.submit_packet(first["packet"], {"1": "共同"})
        second = session.next_group()
        self.assertEqual(second["packet"], first["packet"])
        self.assertEqual(second["pending"], ["2"])
        self.assertIn("second", second["resources"])
        self.assertIn(
            "共同", session.read_resource("second", packet=second["packet"])["text"]
        )

    def test_contradictory_shared_source_rules_are_rejected(self):
        write_json(
            self.input,
            [
                {
                    **MATERIALS[1],
                    "id": str(i),
                    "rules": {"required_terms": {"ここはどこ？": value}},
                }
                for i, value in enumerate(("甲", "乙"))
            ],
        )
        sync_sources(self.project)
        with self.assertRaisesRegex(ValueError, "Incompatible"):
            self.plan()

    def test_submit_checks_placeholders_and_explicit_revision(self):
        sync_sources(self.project, ["scene"])
        self.plan()
        session = setup_session(self.target.work)
        packet = session.next_group()["packet"]
        with self.assertRaisesRegex(ValueError, "placeholders"):
            session.submit_packet(packet, {"2": "欢迎！"})
        session.submit_packet(packet, {"1": "这里是哪？"})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            session.submit_packet(packet, {"1": "这是哪里？"})
        session.revise_packet(
            packet, {"1": {"before": "这里是哪？", "translation": "这是哪里？"}}
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            session.revise_packet(
                packet, {"1": {"before": "这里是哪？", "translation": "别的"}}
            )

    def test_unfinished_packet_and_whole_plan_cannot_finalize(self):
        self.plan()
        session = setup_session(self.target.work)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            session.finish_packet(session.next_group()["packet"])
        with self.assertRaisesRegex(ValueError, "tasks remain"):
            session.finalize()

    def test_answers_resume_after_process_restart_and_checkout_move(self):
        self.plan()
        session = setup_session(self.target.work)
        session.submit_packet(session.next_group()["packet"], {"1": "爱丽丝"})
        self.assertEqual(Session(self.target.work).status()["completed"], 1)
        copied = self.root.parent / "copied"
        shutil.copytree(self.root, copied)
        project = load_project(copied / "translation.toml")
        from workflow.prepare import bind_runtime

        bind_runtime(
            project.targets["zh-Hans"].work, project, project.targets["zh-Hans"]
        )
        self.assertEqual(
            setup_session(project.targets["zh-Hans"].work).status()["completed"], 1
        )

    def test_snapshot_tampering_blocks_setup_and_finish(self):
        self.plan()
        session = setup_session(self.target.work)
        file = next(iter(session.plan.resources.values()))
        write_json(self.project.sources / file, {"tampered": True})
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            setup_session(self.target.work)

    def test_manual_edit_blocks_publication_without_partial_write(self):
        self.plan()
        self.finish()
        write_json(self.target.translations / "ui.json", {"始める": "人工"})
        before = {p: p.read_bytes() for p in self.target.translations.rglob("*.json")}
        with self.assertRaisesRegex(ValueError, "changed since"):
            merge_results(self.project, self.target)
        self.assertEqual(
            before,
            {p: p.read_bytes() for p in self.target.translations.rglob("*.json")},
        )

    def test_partial_publication_recovers_and_second_publish_is_noop(self):
        self.plan()
        self.finish()
        update = prepare_update(self.project, self.target)
        for path, raw in list(update.files.items())[:2]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        merge_results(self.project, self.target)
        self.assertEqual(merge_results(self.project, self.target), 0)
        self.assertEqual(status(self.target)["state"], "published")

    def test_all_targets_preflight_before_any_write(self):
        updates = []
        for target in self.project.targets.values():
            self.plan(target)
            self.finish(target)
            updates.append(prepare_update(self.project, target))
        spanish = self.project.targets["es"]
        write_json(spanish.translations / "manual.json", {"人工": "edited"})
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_updates(updates)
        self.assertFalse((self.target.translations / "ui.json").exists())

    def test_empty_plan_needs_no_backend_or_model(self):
        write_json(self.input, [])
        self.cli("update")
        self.assertEqual(
            read_json(self.target.work / "results.json")["translations"], []
        )

    def test_exported_snapshot_and_restored_results_are_portable(self):
        sync_sources(self.project, ["ui"], export=True)
        self.plan()
        self.finish()
        archive_root = self.root.parent / "downloaded"
        with zipfile.ZipFile(self.project.source_bundle) as archive:
            archive.extractall(archive_root)
        restored = replace(self.project, sources=archive_root)
        self.assertEqual(
            read_snapshot(restored)[0].resources,
            read_snapshot(self.project)[0].resources,
        )
        artifacts = self.root.parent / "artifacts"
        artifacts.mkdir()
        for name in ("plan.json", "results.json", "prepare-report.json"):
            shutil.copyfile(self.target.work / name, artifacts / name)
        restore_artifacts(self.project, [self.target], artifacts)
        merge_results(self.project, self.target)

    def test_agent_must_submit_and_runs_in_bounded_packets(self):
        self.plan()
        calls = []

        def execute(_command, prompt, _log, _timeout):
            session = Session(self.target.work)
            packet = session.next_group()
            self.assertIn(packet["packet"], prompt)
            calls.append(packet["resource"])
            session.submit_packet(
                packet["packet"],
                {
                    str(i): VALUES[task.source]
                    for i, task in enumerate(session.packet_tasks(packet["packet"]), 1)
                },
            )

        backend = replace(self.project.backend(self.target), model="test")
        with (
            patch.dict(os.environ, {"MODEL_API_KEY": "test"}),
            patch("workflow.translate.execute_codex", side_effect=execute),
        ):
            translate_plan(self.target.work, backend)
        self.assertEqual(calls, ["cast", "scene", "ui"])
        self.assertEqual(Session(self.target.work).status()["remaining"], 0)

    def test_agent_final_prose_and_input_mutations_cannot_pass(self):
        self.plan()
        backend = replace(self.project.backend(self.target), model="test")
        with (
            patch.dict(os.environ, {"MODEL_API_KEY": "test"}),
            patch("workflow.translate.execute_codex"),
        ):
            with self.assertRaisesRegex(ValueError, "unresolved"):
                translate_plan(self.target.work, backend)

        def mutate(*args):
            write_json(self.target.translations / "unexpected.json", {"x": "y"})

        with (
            patch.dict(os.environ, {"MODEL_API_KEY": "test"}),
            patch("workflow.translate.execute_codex", side_effect=mutate),
        ):
            with self.assertRaisesRegex(ValueError, "protected"):
                translate_plan(self.target.work, backend)

    def test_term_proposals_require_real_evidence(self):
        self.plan()
        session = setup_session(self.target.work)
        packet = session.next_group()["packet"]
        session.submit_packet(packet, {"1": "爱丽丝"})
        with self.assertRaisesRegex(ValueError, "does not occur"):
            session.propose_packet(
                packet,
                [
                    {
                        "source": "不存在",
                        "translation": "不存在",
                        "note": "test",
                        "evidence": "1",
                    }
                ],
            )

    def test_prune_keeps_current_answers(self):
        self.plan()
        self.finish()
        answer = next((self.target.work / "answers").glob("*.json"))
        os.utime(answer, (0, 0))
        expired = self.target.work / "answers/expired.json"
        write_json(expired, {})
        os.utime(expired, (0, 0))
        prune_cache(self.target, 1)
        self.assertTrue(answer.exists())
        self.assertFalse(expired.exists())


class ResourceFormatTests(unittest.TestCase):
    def test_compact_data_checks_shape_and_output_paths(self):
        for value in (
            "../outside.json",
            "/absolute.json",
            "a\\b.json",
            "manifest.json",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Resource(id="a", output=value, kind="text")
        for lines in ([[None, ""]], [["speaker"]], [[None, "text", "extra"]]):
            with self.assertRaises(ValueError):
                Resource(id="a", output="a.json", kind="dialogue", lines=lines)
        with self.assertRaises(ValueError):
            Resource(id="a", output="a.json", kind="text", blocks=[{"texts": [" "]}])
        with self.assertRaises(ValueError):
            Resource.model_validate({"id": "x", "category": "old-entry-format"})

    def test_initializer_does_not_overwrite_and_validates_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            with self.assertRaises(ValueError):
                create_project(root, targets=["../es"])
            self.assertFalse(root.exists())
            create_project(root)
            with self.assertRaises(FileExistsError):
                create_project(root)
            self.assertEqual(read_json(root / "sources/resources.json"), [])

    def test_new_command_arguments_are_separate(self):
        parser = argument_parser()
        self.assertEqual(parser.parse_args(["plan", "--limit", "2"]).limit, 2)
        self.assertFalse(hasattr(parser.parse_args(["plan"]), "source_id"))
        self.assertEqual(
            parser.parse_args(["sync", "--source-id", "scene"]).source_id, ["scene"]
        )
