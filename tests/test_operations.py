"""Behavioral regressions for portable plans, recovery and daily operations."""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_framework

from scripts.agent import argument_parser as agent_parser
from scripts.build import main as build_main
from scripts.ci import argument_parser as ci_parser
from scripts.ci import restore_artifacts
from scripts.evaluation import evaluate
from scripts.glossary import (
    apply_proposals,
    resolve_glossary,
    term_for,
    terms_payload,
)
from scripts.merge import apply_updates, merge_results, prepare_update
from scripts.models import Results, Task, TermProposal
from scripts.operations import prune_cache, status
from scripts.prepare import compile_catalog, prepare_tasks, source_catalog
from scripts.run import argument_parser, check_translations, main
from scripts.session import Session, setup_session
from scripts.utils import read_json, write_bytes, write_json


class OperationsTests(unittest.TestCase):
    setUp = test_framework.FrameworkTests.setUp
    prepare = test_framework.FrameworkTests.prepare
    translate = test_framework.FrameworkTests.translate

    def test_every_partial_publication_can_resume(self):
        # Two independent term files reproduce mixed before/after terminology.
        entries = read_json(self.root / "source.json")
        entries.append(
            {
                **entries[0],
                "id": "guide",
                "source": "Guide",
                "targets": [{"file": "guides.json", "path": ["Guide"]}],
            }
        )
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        target = self.project.targets["es"]
        self.prepare("es")
        session = setup_session(target.work)
        for phase in (True, False):
            session.submit(
                {
                    "translations": [
                        {"id": t.id, "translation": t.source}
                        for t in session.plan.tasks
                        if t.term == phase
                    ]
                }
            )
        evidence = next(t.id for t in session.plan.tasks if t.source == "Rook")
        session.propose(
            [
                {
                    "source": "Rook",
                    "translation": "Rook",
                    "note": "Character name",
                    "evidence": evidence,
                }
            ]
        )
        session.finalize()
        update = prepare_update(self.project, target)
        expected = dict(update.files)
        self.assertGreaterEqual(len(expected), 6)
        for fail_after in range(1, len(expected) + 1):
            with self.subTest(fail_after=fail_after):
                writes = 0

                def interrupted(path, raw):
                    nonlocal writes
                    write_bytes(path, raw)
                    writes += 1
                    if writes == fail_after:
                        raise OSError("Interrupted publication")

                with patch("scripts.merge.write_bytes", side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, "Interrupted"):
                        apply_updates([prepare_update(self.project, target)])
                merge_results(self.project, target)
                self.assertEqual({p: p.read_bytes() for p in expected}, expected)
                self.assertEqual(merge_results(self.project, target), 0)
                self.assertEqual(status(target)["state"], "published")
                for path in expected:
                    path.unlink()
                (target.work / "publication.json").unlink()

    def test_partial_merge_rejects_unrelated_terminology_edit(self):
        target = self.project.targets["es"]
        write_json(target.translations / "characters.json", {"Unused": "Old"})
        # Publication bindings only expose declared terminology.
        entries = read_json(self.root / "source.json")
        entries.append(
            {
                **entries[0],
                "id": "unused",
                "source": "Unused",
                "targets": [
                    {
                        "file": "characters.json",
                        "path": ["Unused"],
                    }
                ],
            }
        )
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        self.prepare("es")
        self.translate("es")
        write_json(target.translations / "characters.json", {"Unused": "Human change"})
        with self.assertRaisesRegex(ValueError, "Published terminology changed"):
            merge_results(self.project, target)

    def test_self_reference_cleanup_preserves_answers_and_review_policy(self):
        target = self.project.targets["es"]
        write_json(target.translations / "characters.json", {"Rook": "Rook"})
        write_json(target.glossary, {"Rook": {"reference": "Rook"}})
        self.prepare("es")
        before = self.translate("es")
        write_json(target.glossary, {})
        after = setup_session(target.work)
        self.assertEqual(before.config["policy"], after.config["policy"])
        self.assertEqual(before.answers(), after.answers())
        after.finalize()
        merge_results(self.project, target)
        self.prepare("es")
        self.assertNotIn(
            "review_outputs", read_json(target.work / "prepare-report.json")
        )

    def test_output_address_changes_preserve_all_group_request_ids(self):
        before = self.prepare("es")
        entries = read_json(self.root / "source.json")
        entries[1]["targets"][0]["file"] = "relocated/scene.json"
        write_json(self.root / "source.json", entries)
        self.adapter.fetch(self.project.sources, None, self.options)
        after = self.prepare("es")
        self.assertEqual(
            [t["id"] for t in before["tasks"]], [t["id"] for t in after["tasks"]]
        )
        self.assertNotEqual(before["id"], after["id"])
        task = next(t for t in after["tasks"] if t["category"] == "dialogue")
        with patch("scripts.games.json_file.read_json", wraps=read_json) as reads:
            context = self.adapter.context(task, self.project.sources, self.options)
        self.assertEqual(len(context["group"]), 2)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(reads.call_args.args[0].parent.name, "groups")
        self.assertNotIn("targets", context["group"][0])

    def test_shared_catalog_reuses_extraction_and_detects_stale_inputs(self):
        with patch.object(
            self.adapter, "extract", wraps=self.adapter.extract
        ) as extract:
            compiled = compile_catalog(self.project)
            for target in self.project.targets.values():
                prepare_tasks(
                    self.project,
                    target,
                    source_catalog(self.project, self.project.catalog),
                )
            self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(compiled.entries), 4)
        write_json(self.project.sources / "index.json", {"changed": True})
        with self.assertRaisesRegex(ValueError, "catalog is stale"):
            source_catalog(self.project, self.project.catalog)
        self.adapter.fetch(self.project.sources, None, self.options)
        compile_catalog(self.project)
        write_json(self.root / "source.json", [])
        with self.assertRaisesRegex(ValueError, "catalog is stale"):
            source_catalog(self.project, self.project.catalog)

    def test_session_reuses_document_reads_and_projection(self):
        self.prepare("es")
        target = self.project.targets["es"]
        setup_session(target.work)
        with patch(
            "scripts.session.read_optional",
            wraps=lambda p: read_json(p) if p.exists() else {},
        ) as reads:
            session = Session(target.work)
            session.status()
            terms = session.current_terms
            session.status()
            session.next_group()
            self.assertIs(session.current_terms, terms)
            self.assertEqual(reads.call_count, 2)  # glossary and one term document

    def test_setup_reads_each_terminology_file_once(self):
        from scripts.glossary import read_optional

        self.prepare("es")
        reads = Counter()

        def counted(path):
            reads[path] += 1
            return read_optional(path)

        target = self.project.targets["es"]
        with (
            patch("scripts.session.read_optional", side_effect=counted),
            patch("scripts.glossary.read_optional", side_effect=counted),
        ):
            session = setup_session(target.work)
        self.assertEqual(
            reads, {target.glossary: 1, target.translations / "characters.json": 1}
        )
        self.assertNotIn("published_terms_hash", session.config)

    def test_legacy_unused_fingerprint_is_dropped_without_relaxing_other_fields(self):
        self.prepare("es")
        self.translate("es")
        target = self.project.targets["es"]
        result = read_json(target.work / "results.json")
        self.assertNotIn("published_terms_before", result)
        legacy = {**result, "published_terms_before": "obsolete"}
        self.assertEqual(Results.model_validate(legacy).model_dump(), result)
        write_json(target.work / "results.json", legacy)
        merge_results(self.project, target)
        with self.assertRaises(ValueError):
            Results.model_validate({**legacy, "unexpected": "not allowed"})

    def test_publish_restores_only_plan_results_and_reports(self):
        self.prepare("es")
        self.translate("es")
        target = self.project.targets["es"]
        artifact = self.root / "artifacts/translation-es"
        for name in ("plan.json", "results.json", "prepare-report.json"):
            write_json(artifact / name, read_json(target.work / name))
        (artifact / "agent-report.md").write_text("Completed", encoding="utf-8")
        write_json(artifact / "answers/old.json", {"obsolete": True})
        write_json(artifact / "runtime.json", {"translations": "incorrect"})
        restored = replace(target, work=self.root / "restored")
        restore_artifacts(self.project, [restored], artifact.parent)
        self.assertFalse((restored.work / "answers").exists())
        self.assertNotEqual(
            read_json(restored.work / "runtime.json"), {"translations": "incorrect"}
        )
        merge_results(self.project, restored)
        self.assertEqual(status(restored)["state"], "published")
        (artifact / "results.json").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            restore_artifacts(self.project, [restored], artifact.parent)

    def test_publish_restores_single_artifact_from_download_root(self):
        self.prepare("es")
        self.translate("es")
        target = self.project.targets["es"]
        artifact = self.root / "download"
        for name in ("plan.json", "results.json", "prepare-report.json"):
            write_json(artifact / name, read_json(target.work / name))
        restored = replace(target, work=self.root / "restored")
        restore_artifacts(self.project, [restored], artifact)
        merge_results(self.project, restored)
        self.assertEqual(status(restored)["state"], "published")
        with self.assertRaisesRegex(ValueError, "exactly one target"):
            restore_artifacts(
                self.project, list(self.project.targets.values()), artifact
            )
        with self.assertRaisesRegex(ValueError, "another project or language"):
            restore_artifacts(self.project, [self.project.targets["zh-Hans"]], artifact)

    def test_agent_and_ci_commands_have_separate_arguments(self):
        for parser, args in (
            (agent_parser(), ["next"]),
            (agent_parser(), ["setup", "--work", "work"]),
            (
                agent_parser(),
                ["status", "--work", "work", "--config", "translation.toml"],
            ),
            (agent_parser(), ["context", "--work", "work"]),
            (ci_parser(), ["configure"]),
            (
                ci_parser(),
                ["restore", "--artifacts", "artifacts", "--backend", "alpha"],
            ),
        ):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                parser.parse_args(args)
            self.assertEqual(error.exception.code, 2)
        args = agent_parser().parse_args(["context", "id", "--work", "work"])
        self.assertEqual(args.id, "id")
        self.assertEqual(args.work, Path("work"))

    def test_dry_run_prepares_all_targets_without_execution_or_publication(self):
        with (
            patch("scripts.run.translate_plan") as execute,
            patch.object(
                sys,
                "argv",
                [
                    "workflow",
                    "update",
                    "--config",
                    str(self.config),
                    "--dry-run",
                    "--limit",
                    "1",
                ],
            ),
        ):
            main()
        execute.assert_not_called()
        for target in self.project.targets.values():
            self.assertEqual(read_json(target.work / "prepare-report.json")["tasks"], 1)
            self.assertFalse(target.translations.exists())

    def test_commands_reject_inapplicable_options(self):
        parser = argument_parser()
        for args in (
            ["translate", "--limit", "1"],
            ["check", "--backend", "alpha"],
            ["merge", "--dry-run"],
        ):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                parser.parse_args(args)
            self.assertEqual(error.exception.code, 2)

    def test_fresh_work_directory_recovers_terms_without_persistent_state(self):
        target = self.project.targets["es"]
        self.prepare("es")
        self.translate("es")
        merge_results(self.project, target)
        write_json(target.glossary, {"Rook": {"reference": "Rook"}})
        target = replace(target, work=self.root / "fresh-work")
        plan = prepare_tasks(self.project, target)
        self.assertEqual(plan.tasks, [])
        setup_session(target.work).finalize()
        self.assertEqual(merge_results(self.project, target), 0)
        check_translations(self.project, target)
        self.assertFalse((self.root / "translation-state").exists())

    def test_prune_keeps_active_answers_groups_and_proposals(self):
        target = self.project.targets["es"]
        self.prepare("es")
        session = self.translate("es")
        active = list((target.work / "answers").glob("*.json")) + list(
            (target.work / "groups").glob("*.json")
        )
        proposal = target.work / "proposals" / f"{session.plan.id}.json"
        write_json(proposal, [])
        active.append(proposal)
        stale = [
            target.work / directory / "expired.json"
            for directory in ("answers", "groups", "proposals")
        ]
        for path in stale:
            write_json(path, {})
        for path in [*active, *stale]:
            os.utime(path, (1, 1))
        self.assertEqual(len(prune_cache(target)["removed"]), 3)
        self.assertTrue(all(p.exists() for p in active))
        self.assertEqual(prune_cache(target)["removed"], [])

    def test_staged_configuration_controls_manifest_even_if_worktree_is_invalid(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        config = test_framework.CONFIG.replace(
            "[targets.es]",
            '[targets.es]\ntranslations = "published/spanish"\nstyle = "missing-style.md"',
        )
        self.config.write_text(config, encoding="utf-8")
        source = self.root / "published/spanish/ui.json"
        write_json(source, {"start": "Start"})
        subprocess.run(
            ["git", "-C", str(self.root), "add", "."], check=True, capture_output=True
        )
        for unstaged in (
            "invalid toml [",
            config.replace("published/spanish", "other/location"),
            None,
        ):
            if unstaged is None:
                self.config.unlink()
            else:
                self.config.write_text(unstaged, encoding="utf-8")
            with patch.object(
                sys, "argv", ["build", "--staged", "--config", str(self.config)]
            ):
                build_main()
            self.assertIn("ui", read_json(source.parent / "manifest.json"))
            self.assertFalse((self.root / "other/location/manifest.json").exists())

    def test_scoped_terms_override_globals_without_changing_other_categories(self):
        glossary = {
            "Rook": [{"reference": "Rook"}, {"translation": "车", "categories": ["ui"]}]
        }
        terms = resolve_glossary(glossary, {"Rook": "洛克"})
        self.assertEqual(term_for(terms, "Rook", "ui").translation, "车")
        self.assertEqual(term_for(terms, "Rook", "dialogue").translation, "洛克")
        glossary["Rook"].append({"translation": "战车", "categories": ["ui", "chess"]})
        with self.assertRaisesRegex(ValueError, "Overlapping glossary scopes"):
            resolve_glossary(glossary, {"Rook": "洛克"})
        self.assertEqual(
            terms_payload(
                resolve_glossary({"Rook": {"reference": "Rook"}}, {"Rook": "洛克"})
            ),
            terms_payload(resolve_glossary({}, {"Rook": "洛克"})),
        )

    def test_scoped_proposals_require_matching_evidence_and_cannot_change_existing_scope(
        self,
    ):
        task = Task.model_construct(
            id="ui",
            source="Move Rook",
            context={},
            category="ui",
            term=False,
            use_terms=True,
        )
        proposal = TermProposal(
            source="Rook",
            translation="车",
            note="Chess piece",
            evidence="ui",
            categories=["ui"],
        )
        result = apply_proposals([proposal], {"ui": task}, {}, {}, {"Rook": "洛克"}, {})
        self.assertEqual(
            term_for(
                resolve_glossary(result, {"Rook": "洛克"}), "Rook", "ui"
            ).translation,
            "车",
        )
        with self.assertRaisesRegex(ValueError, "Existing term conflict"):
            apply_proposals(
                [proposal.model_copy(update={"translation": "战车"})],
                {"ui": task},
                {},
                result,
                {"Rook": "洛克"},
                {},
            )
        with self.assertRaisesRegex(ValueError, "Evidence category"):
            apply_proposals(
                [proposal.model_copy(update={"categories": ["dialogue"]})],
                {"ui": task},
                {},
                {},
                {},
                {},
            )

    def test_evaluation_separates_hard_rules_reference_match_and_review(self):
        suite, answers = self.root / "suite.json", self.root / "answers.json"
        write_json(
            suite,
            {
                "source_language": "en",
                "target": "es",
                "cases": [
                    {
                        "id": "formal",
                        "source": "Welcome",
                        "reference": "Bienvenido",
                        "review_status": "confirmed",
                    },
                    {"id": "draft", "source": "Road", "reference": "Camino"},
                    {
                        "id": "placeholder",
                        "source": "Hi {0}",
                        "reference": "Hola {0}",
                        "rules": {"protected_patterns": [r"\{\d+\}"]},
                    },
                ],
            },
        )
        write_json(
            answers, {"formal": "Bienvenida", "draft": "Camino", "placeholder": "Hola"}
        )
        report = evaluate(suite, answers)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["reference_matches"], 1)
        self.assertTrue(report["results"][0]["needs_human_review"])
        self.assertTrue(report["results"][1]["needs_human_review"])
        self.assertIsNone(report["results"][0]["error"])
