"""Configuration mistakes and precedence are user-facing behavior."""

import tempfile
import tomllib
import unittest
from pathlib import Path

from workflow.config import load_project
from workflow.translate import codex_command

CONFIG = """schema_version = 1
[project]
id = "test"
source_language = "en"
adapter = "adapters.json_file"
backend = "alpha"
[backends.alpha]
base_url = "https://example.invalid/v1"
api_key_env = "ALPHA_KEY"
model = "first"
[backends.beta]
base_url = "https://example.invalid/second"
api_key_env = "BETA_KEY"
model = "second"
[targets.zh-Hans]
[targets.es]
backend = "beta"
"""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "translation.toml"

    def load(self, text=CONFIG):
        self.file.write_text(text, encoding="utf-8")
        return load_project(self.file)

    def test_scope_overrides_and_environment_precedence(self):
        project = self.load()
        chinese, spanish = project.select()
        self.assertEqual(project.backend(chinese).name, "alpha")
        self.assertEqual(project.backend(spanish).name, "beta")
        env = {"TRANSLATION_BACKEND": "alpha", "TRANSLATION_MODEL": "override"}
        selected = project.backend(spanish, environ=env)
        self.assertEqual(
            (selected.name, selected.model, selected.selected_by),
            ("alpha", "override", "TRANSLATION_BACKEND"),
        )
        selected = project.backend(spanish, "beta", "explicit", environ=env)
        self.assertEqual(
            (selected.name, selected.model, selected.api_key_env),
            ("beta", "explicit", "BETA_KEY"),
        )
        self.assertEqual(
            project.backend(spanish, environ={"TRANSLATION_BACKEND": ""}).name, "beta"
        )
        for arguments in ({"name": ""}, {"model": ""}, {"name": " alpha"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                project.backend(spanish, **arguments)

    def test_unknown_fields_types_and_legacy_config_are_rejected(self):
        for source in (
            CONFIG.replace('backend = "alpha"', 'backned = "alpha"', 1),
            CONFIG.replace("[targets.es]", '[targets.es]\nenabled = "false"'),
            CONFIG.replace("schema_version = 1", 'schema_version = "1"'),
            CONFIG.replace("schema_version = 1", "schema_version = true"),
            CONFIG.replace('model = "first"', "model = false"),
            CONFIG.replace('model = "first"', 'model_env = "LEGACY_MODEL"'),
            CONFIG.replace('backend = "alpha"', 'default_backend = "alpha"', 1),
            CONFIG.replace("[targets.es]", '[targets.es]\nstyle_text = "hidden rule"'),
        ):
            with self.subTest(config=source), self.assertRaises(ValueError):
                self.load(source)

    def test_unused_wrong_reference_and_noncanonical_locale_fail(self):
        with self.assertRaisesRegex(ValueError, "unknown backend"):
            self.load(CONFIG.replace('backend = "alpha"', 'backend = "absent"', 1))
        for code in ("zh_hans", "xx-NotALanguage", "../es"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.load(CONFIG.replace("targets.es", "targets." + code))

    def test_offline_config_needs_no_key_or_model(self):
        project = self.load(CONFIG.replace('model = "first"\n', ""))
        backend = project.backend(project.targets["zh-Hans"])
        self.assertIsNone(backend.model)
        with self.assertRaisesRegex(ValueError, "Set backends.alpha.model"):
            backend.require_model()

    def test_all_declared_targets_or_explicit_subset(self):
        project = self.load()
        self.assertEqual([t.code for t in project.select()], ["zh-Hans", "es"])
        self.assertEqual([t.code for t in project.select(["es"])], ["es"])
        for codes in ([""], ["es,"], ["es", "es"], ["ja"]):
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                project.select(codes)

    def test_context_budget_is_optional_strict_and_used_by_cli(self):
        def configured(value):
            return CONFIG.replace(
                "[backends.beta]",
                f'[backends.alpha.codex]\neffort = "high"\ncontext_window = {value}\n[backends.beta]',
            )

        project = self.load(configured("1_000_000"))
        backend = project.backend(project.targets["zh-Hans"])
        command = codex_command(backend, self.file.parent)
        local = tomllib.loads(
            "\n".join(command[i + 1] for i, arg in enumerate(command) if arg == "-c")
        )
        for key, value in {
            "model_context_window": 1_000_000,
            "model_reasoning_effort": "high",
            "model_reasoning_summary": "none",
        }.items():
            self.assertEqual(local[key], value)
        self.assertNotIn("model_catalog_json", local)
        self.assertIsNone(project.backend(project.targets["es"]).context_window)
        for value in ("0", "-1", "true", '"1000000"', "1.5"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "context_window"),
            ):
                self.load(configured(value))
        with self.assertRaisesRegex(ValueError, "model_catalog"):
            self.load(
                CONFIG.replace(
                    "[backends.beta]",
                    '[backends.alpha.codex]\nmodel_catalog = "legacy.json"\n[backends.beta]',
                )
            )


if __name__ == "__main__":
    unittest.main()
