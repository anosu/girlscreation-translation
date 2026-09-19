"""Exercise batching, context, term guidance and stdin through the public workflow."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from workflow.agent import main as agent_main
from workflow.config import load_project
from workflow.merge import merge_results
from workflow.packets import TEXT_CHARS, TEXT_ITEMS
from workflow.prepare import prepare_tasks
from workflow.scaffold import create_project
from workflow.session import setup_session
from workflow.snapshot import sync_sources
from workflow.utils import read_json, write_json


def text(id, output, texts, path=None, **extra):
    return {
        "id": id,
        "output": output,
        "path": path or [],
        "kind": "text",
        "blocks": [{"texts": texts}],
        **extra,
    }


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "game"
        self.config = create_project(self.root)
        self.project = load_project(self.config)
        self.target = self.project.targets["zh-Hans"]

    def plan(self, resources):
        write_json(self.root / "sources/resources.json", resources)
        sync_sources(self.project)
        return prepare_tasks(self.project, self.target)

    def finish(self, session):
        while session.status()["remaining"]:
            packet = session.next_group()
            session.submit_packet(
                packet["packet"],
                {
                    str(i): "译" + task.source
                    for i, task in enumerate(session.packet_tasks(packet["packet"]), 1)
                },
            )
            session.finish_packet(packet["packet"])
        session.finalize()
        merge_results(self.project, self.target)

    def test_names_scene_and_related_texts_use_three_sessions_not_six(self):
        plan = self.plan(
            [
                text("names", "names.json", ["アリス"], term=True),
                {
                    "id": "story",
                    "output": "novels/1.json",
                    "kind": "dialogue",
                    "lines": [["アリス", "こんにちは。"], [None, "風が吹いた。"]],
                },
                text("title", "novels/1.json", ["出会い"]),
                text("items/name", "master.json", ["道具"], ["mItems", "name"]),
                text(
                    "items/description",
                    "master.json",
                    ["使う"],
                    ["mItems", "description"],
                ),
                text("ui", "ui.json", ["閉じる"]),
            ]
        )
        self.assertEqual(
            list(plan.packets.values()),
            [["names"], ["story", "title"], ["items/name", "items/description", "ui"]],
        )
        report = read_json(self.target.work / "prepare-report.json")
        self.assertEqual((report["packets"], report["resource_packets"]), (3, 6))
        session = setup_session(self.target.work)
        self.assertEqual(session.next_group()["resources"], ["names"])
        self.finish(session)
        self.assertEqual(
            read_json(self.target.translations / "novels/1.json")["出会い"], "译出会い"
        )
        self.assertEqual(
            read_json(self.target.translations / "master.json")["mItems"]["name"],
            {"道具": "译道具"},
        )

    def test_whole_scenes_stay_separate_even_above_text_batch_size(self):
        scenes = [
            {
                "id": f"scene-{n}",
                "output": f"novels/{n}.json",
                "kind": "dialogue",
                "lines": [[None, f"台詞{i}"] for i in range(TEXT_ITEMS + 5)],
            }
            for n in range(2)
        ]
        plan = self.plan(scenes)
        self.assertEqual(len(plan.packets), 2)
        session = setup_session(self.target.work)
        packet = session.next_group()
        self.assertEqual(len(session.packet_tasks(packet["packet"])), TEXT_ITEMS + 5)
        self.assertEqual(packet["resources"], ["scene-0"])

    def test_long_table_splits_and_only_renders_current_batch_by_default(self):
        rows = [f"項目{i:04d}" for i in range(TEXT_ITEMS * 2 + 1)]
        plan = self.plan([text("items", "master.json", rows, ["mItems", "name"])])
        self.assertEqual(len(plan.packets), 3)
        session = setup_session(self.target.work)
        packet = session.next_group()
        self.assertEqual(len(session.packet_tasks(packet["packet"])), TEXT_ITEMS)
        self.assertIn(rows[0], packet["page"]["text"])
        self.assertNotIn(rows[-1], packet["page"]["text"])
        self.assertIn(rows[-1], session.read_resource("items", limit=64000)["text"])
        self.finish(session)
        self.assertEqual(
            len(read_json(self.target.translations / "master.json")["mItems"]["name"]),
            len(rows),
        )

    def test_missing_title_receives_completed_dialogue_context(self):
        write_json(
            self.target.translations / "novels/1.json",
            {"会えてよかった。": "能见到你真好。"},
        )
        self.plan(
            [
                {
                    "id": "story",
                    "output": "novels/1.json",
                    "kind": "dialogue",
                    "lines": [[None, "会えてよかった。"]],
                },
                text("title", "novels/1.json", ["再会"]),
            ]
        )
        packet = setup_session(self.target.work).next_group()
        self.assertEqual(packet["resources"], ["story", "title"])
        self.assertIn("能见到你真好。", packet["page"]["text"])

    def test_text_batches_respect_source_size_as_well_as_item_count(self):
        rows = [str(i) + "長" * (TEXT_CHARS // 2) for i in range(3)]
        plan = self.plan([text("long", "ui.json", rows)])
        self.assertEqual(len(plan.packets), 3)

    def test_explicit_required_translation_still_applies_to_ordinary_text(self):
        self.plan(
            [
                text(
                    "ui",
                    "ui.json",
                    ["開始"],
                    rules={"required_terms": {"開始": "开始"}},
                )
            ]
        )
        session = setup_session(self.target.work)
        packet = session.next_group()["packet"]
        with self.assertRaisesRegex(ValueError, "canonical"):
            session.submit_packet(packet, {"1": "启动"})
        session.submit_packet(packet, {"1": "开始"})

    def test_ordinary_title_is_not_auto_filled_or_forced_to_a_character_name(self):
        write_json(self.target.translations / "names.json", {"同文": "角色名称"})
        plan = self.plan(
            [
                text("names", "names.json", ["同文"], term=True),
                text("title", "titles.json", ["同文"]),
            ]
        )
        self.assertEqual(len(plan.tasks), 1)
        self.assertIsNone(plan.tasks[0].reuse)
        session = setup_session(self.target.work)
        self.assertEqual(session.status()["remaining"], 1)
        packet = session.next_group()
        session.submit_packet(packet["packet"], {"1": "剧情标题"})
        session.finalize()
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "titles.json"), {"同文": "剧情标题"}
        )

    def test_submission_from_stdin_needs_no_draft_file(self):
        self.plan([text("ui", "ui.json", ["始める"])])
        packet = setup_session(self.target.work).next_group()
        output = io.StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "agent",
                    "submit",
                    "-",
                    "--packet",
                    packet["packet"],
                    "--work",
                    str(self.target.work),
                ],
            ),
            patch("sys.stdin", io.StringIO('{"1":"开始"}')),
            redirect_stdout(output),
        ):
            agent_main()
        self.assertEqual(json.loads(output.getvalue())["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
