"""Exercise another game, two locales and two model backends through the public workflow."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.adapters import load_adapter
from scripts.ci import matrix
from scripts.codex import configure_action
from scripts.config import ROOT, load_project
from scripts.merge import apply_updates, merge_results, prepare_update
from scripts.prepare import prepare_tasks
from scripts.run import main
from scripts.session import setup_session
from scripts.translate import codex_command
from scripts.utils import read_json, write_json
from scripts.validate import validate_translation

CONFIG = """
schema_version = 1
[project]
id = "other-game"
name = "Another adventure"
source_language = "en"
adapter = "scripts.games.json_file"
backend = "alpha"
[adapter]
input = "source.json"
[backends.alpha]
base_url = "https://alpha.example/v1"
api_key_env = "ALPHA_KEY"
model = "alpha-model"
[backends.beta]
base_url = "https://beta.example/api"
api_key_env = "BETA_KEY"
model = "beta-model"
[targets.zh-Hans]
name = "简体中文"
[targets.es]
name = "Español"
backend = "beta"
"""

TRANSLATIONS = {
    "zh-Hans": {
        "Rook": "洛克",
        "Welcome, {player}!": "欢迎，{player}！",
        "Take the northern road.": "走北边的路。",
        "Start game": "开始游戏",
        "Continue game": "继续游戏",
    },
    "es": {
        "Rook": "Rook",
        "Welcome, {player}!": "¡Bienvenido, {player}!",
        "Take the northern road.": "Toma el camino del norte.",
        "Start game": "Iniciar partida",
        "Continue game": "Continuar partida",
    },
}


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "translation.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        write_json(
            self.root / "source.json", read_json(ROOT / "examples/portable/source.json")
        )
        self.project = load_project(self.config)
        self.adapter = load_adapter(self.project.adapter)
        self.options = {**self.project.options, "root": str(self.project.root)}
        self.adapter.fetch(self.project.sources, None, self.options)

    def prepare(self, code):
        language = self.project.targets[code]
        return prepare_tasks(self.project, language).model_dump()

    def translate(self, code):
        language = self.project.targets[code]
        session = setup_session(language.work)
        while session.status()["remaining"]:
            group = session.next_group()
            tasks = read_json(Path(group["task_file"]))["tasks"]
            session.submit(
                {
                    "translations": [
                        {
                            "id": task["id"],
                            "translation": TRANSLATIONS[code][task["source"]],
                        }
                        for task in tasks
                    ]
                }
            )
        session.finalize()
        return session

    def test_two_locales_keep_ids_terms_results_and_publications_separate(self):
        chinese = self.prepare("zh-Hans")
        spanish = self.prepare("es")
        self.assertFalse(
            {t["id"] for t in chinese["tasks"]} & {t["id"] for t in spanish["tasks"]}
        )
        zh_session = self.translate("zh-Hans")
        es = self.project.targets["es"]
        es_session = setup_session(es.work)
        with self.assertRaisesRegex(ValueError, "unknown task ID"):
            es_session.submit(
                {
                    "translations": [
                        {
                            "id": next(iter(zh_session.answers())),
                            "translation": "wrong locale",
                        }
                    ]
                }
            )
        self.translate("es")
        for code in TRANSLATIONS:
            language = self.project.targets[code]
            merge_results(self.project, language)
            self.assertEqual(
                read_json(language.translations / "interface.json")["menu"]["start"],
                TRANSLATIONS[code]["Start game"],
            )
            self.assertEqual(self.prepare(code)["tasks"], [])
        self.assertNotEqual(
            self.project.targets["es"].state, self.project.targets["zh-Hans"].state
        )
        self.assertIn(
            "Target language: es",
            (es.work / "agent-prompt.md").read_text(encoding="utf-8"),
        )

    def test_stable_key_source_changes_are_not_skipped(self):
        self.prepare("es")
        self.translate("es")
        language = self.project.targets["es"]
        merge_results(self.project, language)
        entries = read_json(self.root / "source.json")
        entries[-1]["source"] = "Continue game"
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        plan = self.prepare("es")
        self.assertEqual(len(plan["tasks"]), 1)
        self.assertEqual(plan["tasks"][0]["source"], "Continue game")
        self.translate("es")
        merge_results(self.project, language)
        self.assertEqual(
            read_json(language.translations / "interface.json")["menu"]["start"],
            "Continuar partida",
        )
        self.assertEqual(self.prepare("es")["tasks"], [])

    def test_logical_context_and_safe_group_filenames(self):
        plan = self.prepare("es")
        language = self.project.targets["es"]
        session = setup_session(language.work)
        name = plan["tasks"][0]
        session.submit({"translations": [{"id": name["id"], "translation": "Rook"}]})
        group = session.next_group()
        self.assertEqual(group["group"], "chapter 1 / arrival")
        self.assertTrue(Path(group["task_file"]).is_relative_to(language.work))
        task = next(t for t in plan["tasks"] if t["category"] == "dialogue")
        self.assertEqual(len(session.context(task["id"])["group"]), 2)

    def test_model_profiles_select_urls_and_keys_without_a_catalog(self):
        provider = self.project.backend(self.project.targets["es"])
        command = codex_command(provider, self.root)
        self.assertEqual(command[command.index("--model") + 1], "beta-model")
        self.assertTrue(any("BETA_KEY" in arg for arg in command))
        self.assertTrue(any("https://beta.example/api" in arg for arg in command))
        self.assertFalse(any(arg.startswith("model_catalog_json=") for arg in command))
        home = self.root / "codex-home"
        configure_action(home, provider)
        self.assertNotIn("model_catalog_json", (home / "config.toml").read_text())
        with patch.dict(os.environ, {"MODEL_API_KEY": "private-test-value"}):
            # Use an in-checkout example for CI's path containment requirement.
            example = load_project(ROOT / "examples/portable/translation.toml")
            payload = matrix(example, model="example-model")
            self.assertEqual(len(payload["include"]), 2)
            self.assertNotIn("private-test-value", json.dumps(payload))

    def test_language_rules_are_optional_and_do_not_leak_chinese_names(self):
        validate_translation("エスティー", "Esty")
        with self.assertRaises(ValueError):
            validate_translation(
                "エスティー",
                "Esty",
                read_json(
                    ROOT / "scripts/games/girlscreation/resources/zh-Hans.rules.json"
                ),
            )
        with self.assertRaisesRegex(ValueError, "configured tags/placeholders"):
            validate_translation(
                "Hello [player]", "Hola", {"protected_patterns": [r"\[\w+\]"]}
            )

    def test_source_language_change_invalidates_pending_answer_ids(self):
        original = self.prepare("es")
        self.config.write_text(
            CONFIG.replace('source_language = "en"', 'source_language = "de"'),
            encoding="utf-8",
        )
        self.project = load_project(self.config)
        revised = self.prepare("es")
        self.assertFalse(
            {t["id"] for t in original["tasks"]} & {t["id"] for t in revised["tasks"]}
        )

    def test_rejects_overlapping_languages_and_unsupported_protocol(self):
        self.config.write_text(
            CONFIG.replace(
                "[targets.es]",
                '[targets.es]\nwork = ".cache/translation/other-game/work-v5/zh-Hans"',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            load_project(self.config)
        self.config.write_text(
            CONFIG.replace(
                "[backends.beta]", '[backends.beta]\nwire_api = "chat_completions"'
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "wire_api"):
            load_project(self.config)

    def test_update_fetches_once_for_multiple_languages(self):
        def fake_agent(work, backend, timeout, *, session=None):
            code = read_json(work / "plan.json")["language"]
            self.translate(code)

        with (
            patch.object(self.adapter, "fetch", wraps=self.adapter.fetch) as fetch,
            patch("scripts.run.translate_plan", side_effect=fake_agent),
            patch.object(sys, "argv", ["run", "update", "--config", str(self.config)]),
        ):
            main()
            self.assertEqual(fetch.call_count, 1)
        for language in self.project.targets.values():
            self.assertTrue((language.translations / "manifest.json").exists())

    def test_core_and_json_adapter_do_not_import_unity_dependencies(self):
        script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'UnityPy', 'Crypto', 'httpx'}:
        raise AssertionError('Unexpected game dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import scripts.run
import scripts.games.json_file
import scripts.games.girlscreation.adapter
"""
        subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)

    def test_copied_checkout_resumes_without_old_machine_paths(self):
        plan = self.prepare("es")
        target = self.project.targets["es"]
        session = setup_session(target.work)
        name = next(t for t in plan["tasks"] if t["term"])
        session.submit({"translations": [{"id": name["id"], "translation": "Rook"}]})
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory)
            shutil.copytree(self.root, copied, dirs_exist_ok=True)
            project = load_project(copied / "translation.toml")
            resumed = setup_session(project.targets["es"].work)
            self.assertEqual(resumed.plan.id, plan["id"])
            self.assertIn(name["id"], resumed.answers())
            self.assertTrue(resumed.cache.is_relative_to(copied))
            self.assertNotIn(str(self.root), resumed.plan.model_dump_json())

    def test_context_changes_invalidate_pending_answers(self):
        initial = self.prepare("es")
        self.translate("es")
        entries = read_json(self.root / "source.json")
        entries[1]["context"]["speaker"] = "Someone else"
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        revised = self.prepare("es")
        before = {t["entry_id"]: t["id"] for t in initial["tasks"]}
        after = {t["entry_id"]: t["id"] for t in revised["tasks"]}
        self.assertEqual(before["menu-start"], after["menu-start"])
        self.assertNotEqual(before["arrival-2"], after["arrival-2"])
        session = setup_session(self.project.targets["es"].work)
        self.assertEqual(session.status()["remaining"], 2)

    def test_source_snapshot_change_blocks_finalize(self):
        self.prepare("es")
        session = self.translate("es")
        entries = read_json(self.project.sources / "entries.json")
        entries[-1]["source"] = "Changed during agent execution"
        write_json(self.project.sources / "entries.json", entries)
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            session.finalize()

    def test_one_target_failure_publishes_neither_target(self):
        def run_agent(work, backend, timeout, *, session=None):
            if read_json(work / "plan.json")["language"] == "es":
                raise ValueError("backend unavailable")
            self.translate("zh-Hans")

        with (
            patch("scripts.run.translate_plan", side_effect=run_agent),
            patch.object(sys, "argv", ["run", "update", "--config", str(self.config)]),
        ):
            with self.assertRaises(SystemExit):
                main()
        self.assertFalse(
            any(t.translations.exists() for t in self.project.targets.values())
        )
        self.assertTrue(
            (self.project.targets["zh-Hans"].work / "results.json").exists()
        )

    def test_all_targets_are_preflighted_before_writing(self):
        updates = []
        for code in self.project.targets:
            self.prepare(code)
            self.translate(code)
            updates.append(prepare_update(self.project, self.project.targets[code]))
        edited = self.project.targets["es"].translations / "manual.json"
        write_json(edited, {"keep": "human text"})
        with self.assertRaisesRegex(ValueError, "changed during preflight"):
            apply_updates(updates)
        self.assertFalse(self.project.targets["zh-Hans"].translations.exists())
        self.assertEqual(read_json(edited), {"keep": "human text"})

    def test_same_text_in_distinct_contexts_has_distinct_tasks(self):
        entries = read_json(self.root / "source.json")
        entries[2]["source"] = entries[1]["source"]
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        plan = self.prepare("es")
        matches = [t for t in plan["tasks"] if t["source"] == entries[1]["source"]]
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(matches[0]["id"], matches[1]["id"])

    def test_publication_change_does_not_change_request_identity(self):
        plan = self.prepare("es")
        before = next(t for t in plan["tasks"] if t["entry_id"] == "menu-start")
        write_json(
            self.project.targets["es"].translations / "interface.json",
            {"menu": {"start": "Unknown old version"}},
        )
        revised = self.prepare("es")
        after = next(t for t in revised["tasks"] if t["entry_id"] == "menu-start")
        self.assertEqual(before["id"], after["id"])
        self.assertNotEqual(plan["id"], revised["id"])
        self.assertEqual(after["reason"], "source_changed")
        self.assertIsNone(after["reuse"])

    def test_game_constraints_are_opt_in(self):
        validate_translation("First\nSecond", "Una línea")
        validate_translation("value < 3", "valor menor que tres")

    def test_policy_change_reports_review_without_retranslating_publications(self):
        from dataclasses import replace

        target = self.project.targets["es"]
        self.prepare("es")
        self.translate("es")
        merge_results(self.project, target)
        changed = replace(target, style="Use a more formal register.")
        plan = prepare_tasks(self.project, changed)
        self.assertEqual(plan.tasks, [])
        report = read_json(changed.work / "prepare-report.json")
        self.assertIn("interface.json", report["review_outputs"])
        setup_session(changed.work).finalize()
        merge_results(self.project, changed)
        self.assertIn("interface.json", read_json(changed.state)["review_outputs"])
        self.assertEqual(
            read_json(changed.translations / "interface.json")["menu"]["start"],
            "Iniciar partida",
        )

    def test_stale_output_is_reported_and_preserved(self):
        target = self.project.targets["es"]
        write_json(target.translations / "retired.json", {"old": "Keep this"})
        self.prepare("es")
        report = read_json(target.work / "prepare-report.json")
        self.assertIn(
            {"file": "retired.json", "path": ["old"]}, report["unmapped_outputs"]
        )
        self.assertTrue((target.translations / "retired.json").exists())

    def test_no_tasks_needs_no_backend_or_key(self):
        for code, target in self.project.targets.items():
            self.prepare(code)
            self.translate(code)
            merge_results(self.project, target)
        self.config.write_text(
            CONFIG.replace('backend = "alpha"\n', "").replace('backend = "beta"\n', ""),
            encoding="utf-8",
        )
        with (
            patch("scripts.run.translate_plan") as execute,
            patch.object(sys, "argv", ["run", "update", "--config", str(self.config)]),
        ):
            main()
        execute.assert_not_called()

    def test_shared_group_name_does_not_bypass_terminology(self):
        entries = read_json(self.root / "source.json")
        for entry in entries:
            entry["group"] = "shared"
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        self.prepare("es")
        session = setup_session(self.project.targets["es"].work)
        group = read_json(Path(session.next_group()["task_file"]))
        self.assertTrue(all(t["term"] for t in group["tasks"]))


if __name__ == "__main__":
    unittest.main()
