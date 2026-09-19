"""Onboarding and cache upgrades through the same public workflow."""

import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import tomli_w

from workflow.cli import main
from workflow.config import load_project
from workflow.merge import merge_results
from workflow.prepare import prepare_tasks, read_plan
from workflow.resources import Resource
from workflow.scaffold import create_project
from workflow.session import setup_session
from workflow.snapshot import sync_sources
from workflow.utils import read_json, write_json


def material(output="ui.json", **options):
    return {
        "output": output,
        "kind": "text",
        "blocks": [{"texts": ["はじめる"]}],
        **options,
    }


class SimplificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "game"
        self.config = create_project(self.root)
        self.settings = tomllib.loads(self.config.read_text(encoding="utf-8"))

    def load(self):
        self.config.write_text(tomli_w.dumps(self.settings), encoding="utf-8")
        project = load_project(self.config)
        return project, project.targets["zh-Hans"]

    def test_optional_ids_are_stable_and_namespaces_are_distinct(self):
        root = Resource.model_validate(material())
        nested = Resource.model_validate(material(path=["table", "name"]))
        self.assertEqual(root.id, "ui.json")
        self.assertEqual(nested.id, 'ui.json#["table","name"]')
        self.assertEqual(Resource.model_validate(nested.model_dump()).id, nested.id)
        for invalid in ("", " ", None):
            with self.subTest(id=invalid), self.assertRaises(ValueError):
                Resource.model_validate(material(id=invalid))

    def test_object_array_and_directory_inputs_use_one_resource_contract(self):
        self.settings.pop("adapter")
        project, _ = self.load()
        source = self.root / "sources/resources.json"
        for value in (material(), [material()]):
            write_json(source, value)
            self.assertEqual(list(sync_sources(project).resources), ["ui.json"])
        write_json(self.root / "sources/other.json", material("titles.json"))
        write_json(self.root / "sources/nested/ignored.json", {"not": "a resource"})
        self.settings["adapter"] = {"input": "sources"}
        project, _ = self.load()
        self.assertEqual(
            set(sync_sources(project).resources), {"ui.json", "titles.json"}
        )
        write_json(self.root / "sources/duplicate.json", material())
        with self.assertRaisesRegex(ValueError, "provide distinct id"):
            sync_sources(project)
        write_json(self.root / "sources/duplicate.json", material(id="another-scene"))
        self.assertEqual(len(sync_sources(project).resources), 3)

    def test_failed_import_retains_snapshot_and_identifies_bad_file(self):
        project, _ = self.load()
        source = self.root / "sources/resources.json"
        write_json(source, material())
        before = sync_sources(project)
        write_json(source, {"kind": "unknown"})
        with self.assertRaisesRegex(ValueError, "Invalid resource in .*resources.json"):
            sync_sources(project)
        plan = prepare_tasks(project, project.targets["zh-Hans"])
        self.assertEqual(plan.resources.keys(), before.resources.keys())

    def test_cache_root_is_derived_and_explicit_legacy_paths_still_work(self):
        self.settings["project"]["cache"] = "../shared-cache"
        first, target = self.load()
        expected = (self.root.parent / "shared-cache/game").resolve()
        self.assertTrue(first.sources.is_relative_to(expected))
        self.assertTrue(target.work.is_relative_to(expected))
        self.settings["project"]["id"] = "another"
        second, other = self.load()
        self.assertNotEqual(first.sources, second.sources)
        self.assertNotEqual(target.work, other.work)
        self.settings["project"]["sources"] = "legacy-sources"
        self.settings["targets"]["zh-Hans"]["work"] = "legacy-work"
        project, target = self.load()
        self.assertEqual(project.sources, self.root / "legacy-sources")
        self.assertEqual(target.work, self.root / "legacy-work")

    def test_offline_onboarding_needs_no_backend_and_plan_explains_sync(self):
        self.settings["project"].pop("backend")
        self.settings.pop("backends")
        project, target = self.load()
        output = io.StringIO()
        with (
            patch("sys.argv", ["workflow", "config", "--config", str(self.config)]),
            patch.dict("os.environ", {}, clear=True),
            redirect_stdout(output),
        ):
            main()
        self.assertIsNone(json.loads(output.getvalue())[0]["backend"])
        with self.assertRaisesRegex(ValueError, "No local snapshot.*workflow -- sync"):
            prepare_tasks(project, target)

    def test_replan_in_place_preserves_applicable_drafts_and_existing_dictionary(self):
        project, target = self.load()
        write_json(self.root / "sources/resources.json", material())
        write_json(target.translations / "ui.json", {"旧文": "旧译"})
        sync_sources(project)
        plan = prepare_tasks(project, target)
        session = setup_session(target.work)
        packet = session.next_group()
        session.submit_packet(packet["packet"], {"1": "开始"})
        old = read_json(target.work / "plan.json")
        old["version"] = 9
        write_json(target.work / "plan.json", old)
        with self.assertRaisesRegex(ValueError, "keep the existing work directory"):
            read_plan(target.work)
        rebuilt = prepare_tasks(project, target)
        self.assertEqual(rebuilt.tasks[0].id, plan.tasks[0].id)
        resumed = setup_session(target.work)
        self.assertEqual(resumed.answers(), {plan.tasks[0].id: "开始"})
        resumed.finalize()
        merge_results(project, target)
        self.assertEqual(
            read_json(target.translations / "ui.json"),
            {"旧文": "旧译", "はじめる": "开始"},
        )

    def test_term_metadata_is_per_dictionary_even_without_missing_names(self):
        project, target = self.load()
        names = {f"名前{i}": f"名称{i}" for i in range(1000)}
        write_json(
            self.root / "sources/resources.json",
            material("names.json", term=True, blocks=[{"texts": list(names)}]),
        )
        sync_sources(project)
        plan = prepare_tasks(project, target)
        self.assertEqual(len(plan.tasks), 1000)
        self.assertEqual(len(plan.term_dictionaries), 1)
        write_json(target.translations / "names.json", names)
        plan = prepare_tasks(project, target)
        self.assertEqual(plan.tasks, [])
        self.assertEqual(len(plan.dictionaries), 1)
        session = setup_session(target.work)
        self.assertEqual(session.projected_names({}), names)


if __name__ == "__main__":
    unittest.main()
