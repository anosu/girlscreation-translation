"""Agent-facing resource batches and complete-scene context."""

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
from workflow.prepare import prepare_tasks
from workflow.scaffold import create_project
from workflow.session import setup_session
from workflow.snapshot import read_resource, sync_sources
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


class AgentWorkflowTests(unittest.TestCase):
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

    def test_one_batch_preserves_names_story_title_and_nested_master(self):
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
            ]
        )
        self.assertEqual(len(plan.tasks), 6)
        self.assertNotIn("windows", plan.model_dump())
        story = read_resource(self.project.sources, plan.resources["story"])
        self.assertEqual(
            [row["source"] for row in story.occurrences()],
            ["こんにちは。", "風が吹いた。"],
        )
        session = setup_session(self.target.work)
        status = session.submit_resources(
            {
                "names": {"アリス": "爱丽丝"},
                "story": {"こんにちは。": "你好。", "風が吹いた。": "起风了。"},
                "title": {"出会い": "相遇"},
                "items/name": {"道具": "道具"},
                "items/description": {"使う": "使用"},
            }
        )
        self.assertEqual(status["remaining"], 0)
        session.finalize()
        merge_results(self.project, self.target)
        self.assertEqual(
            read_json(self.target.translations / "novels/1.json")["出会い"], "相遇"
        )
        self.assertEqual(
            read_json(self.target.translations / "master.json")["mItems"]["name"],
            {"道具": "道具"},
        )

    def test_complete_long_source_is_read_without_framework_pagination(self):
        source = "長" * 40000
        plan = self.plan([text("long", "ui.json", [source])])
        resource = read_resource(self.project.sources, plan.resources["long"])
        self.assertEqual([row["source"] for row in resource.occurrences()], [source])
        session = setup_session(self.target.work)
        session.submit_resources({"long": {source: "长" * 40000}})
        self.assertEqual(session.finalize()["remaining"], 0)

    def test_stdin_submission_uses_source_keys_and_one_answer_file(self):
        self.plan([text("ui", "ui.json", ["始める"])])
        setup_session(self.target.work)
        output = io.StringIO()
        with (
            patch(
                "sys.argv", ["agent", "submit", "-", "--work", str(self.target.work)]
            ),
            patch("sys.stdin", io.StringIO('{"ui":{"始める":"开始"}}')),
            redirect_stdout(output),
        ):
            agent_main()
        self.assertEqual(json.loads(output.getvalue())["remaining"], 0)
        self.assertEqual(
            len(read_json(self.target.work / "answers.json")["translations"]), 1
        )
        self.assertFalse((self.target.work / "answers").exists())

    def test_unknown_source_and_conflicting_shared_source_are_rejected(self):
        self.plan(
            [
                text("a", "ui.json", ["同文"]),
                text("b", "ui.json", ["同文"]),
            ]
        )
        session = setup_session(self.target.work)
        with self.assertRaisesRegex(ValueError, "not pending"):
            session.submit_resources({"a": {"不存在": "错误"}})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            session.submit_resources({"a": {"同文": "译法甲"}, "b": {"同文": "译法乙"}})
        self.assertEqual(session.status()["remaining"], 1)
        session.submit_resources({"a": {"同文": "译法甲"}})
        session.submit_resources({"b": {"同文": "译法乙"}})
        self.assertEqual(session.finalize()["remaining"], 0)
        self.assertEqual(session.answers()[session.plan.tasks[0].id], "译法乙")

    def test_required_term_is_checked_at_submission(self):
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
        with self.assertRaisesRegex(ValueError, "canonical"):
            session.submit_resources({"ui": {"開始": "启动"}})
        self.assertEqual(
            session.submit_resources({"ui": {"開始": "开始"}})["remaining"], 0
        )

    def test_replan_ignores_answers_for_removed_tasks(self):
        self.plan([text("old", "ui.json", ["旧文"])])
        setup_session(self.target.work).submit_resources({"old": {"旧文": "旧译"}})
        self.plan([text("new", "ui.json", ["新文"])])
        session = setup_session(self.target.work)
        self.assertEqual(session.status()["remaining"], 1)
        session.submit_resources({"new": {"新文": "新译"}})
        self.assertEqual(session.finalize()["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
