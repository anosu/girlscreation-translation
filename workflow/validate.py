"""Validate model output before it can enter published translation files."""

import re
from collections import Counter
from typing import Any

from workflow.models import Submission, Task

PROTECTED = re.compile(
    r"<[^>]+>|\{[^{}\r\n]+\}|%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[sdif]|\\[nrt]"
)
TAGS = re.compile(r"<[^>]+>")
NAME_SYMBOLS = re.compile(
    r"(?<![A-Za-zＡ-Ｚａ-ｚ])[A-ZＡ-Ｚ](?![A-Za-zＡ-Ｚａ-ｚ])|[0-9０-９＆&？?（）()]"
)
NUMBERS = re.compile(r"\d+(?:[.,]\d+)*[%％]?")


def combine_rules(game: dict, target: dict) -> dict:
    """Target settings may add constraints, but never erase game format requirements."""
    result = {**game, **target}
    original_terms = game.get("required_terms", {})
    additional_terms = target.get("required_terms", {})
    if any(
        key in original_terms and original_terms[key] != value
        for key, value in additional_terms.items()
    ):
        raise ValueError("Incompatible required term rules")
    result["required_terms"] = {**original_terms, **additional_terms}
    for key in (
        "protected_patterns",
        "name_kinds",
        "number_kinds",
        "forbidden_translations",
    ):
        result[key] = list(dict.fromkeys([*game.get(key, []), *target.get(key, [])]))
    for key in ("preserve_tags", "preserve_newlines"):
        result[key] = game.get(key, False) or target.get(key, False)
    return result


def validate_translation(
    source: str,
    translation: Any,
    rules: dict | None = None,
    *,
    category: str | None = None,
) -> None:
    """Check universal controls and optional target-language terminology rules."""
    rules = rules or {}
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError("Translation must be a nonempty string")
    if rules.get("preserve_tags") and TAGS.findall(source) != TAGS.findall(translation):
        raise ValueError(f"Changed tag order in {source[:80]!r}")
    if rules.get("preserve_newlines") and source.count("\n") != translation.count("\n"):
        raise ValueError(f"Changed literal newlines in {source[:80]!r}")
    for pattern in rules.get("protected_patterns", []):
        if Counter(m.group(0) for m in re.finditer(pattern, source)) != Counter(
            m.group(0) for m in re.finditer(pattern, translation)
        ):
            raise ValueError(f"Changed configured tags/placeholders: {pattern}")
    for forbidden in rules.get("forbidden_translations", []):
        if forbidden in translation:
            raise ValueError(f"Forbidden target-language spelling: {forbidden}")
    for original, required in rules.get("required_terms", {}).items():
        if original == source and required != translation:
            raise ValueError(f"Missing canonical translation {required}")
    for kind, pattern in (
        ("name_kinds", rules.get("name_identifier_pattern", NAME_SYMBOLS.pattern)),
        ("number_kinds", NUMBERS.pattern),
    ):
        if category in rules.get(kind, []) and Counter(
            m.group(0) for m in re.finditer(pattern, PROTECTED.sub("", source))
        ) != Counter(
            m.group(0) for m in re.finditer(pattern, PROTECTED.sub("", translation))
        ):
            raise ValueError(
                f"Changed name identifiers or master numbers: {source[:80]!r}"
            )


def validate_published(document: dict, rules: dict) -> int:
    count = 0
    for source, value in document.items():
        if isinstance(value, dict):
            count += validate_published(value, rules)
        else:
            validate_translation(source, value, rules)
            count += 1
    return count


def changed_translations(document: dict, before: dict) -> dict:
    """Keep only added or changed strings, preserving their dictionary paths."""
    changed = {}
    for source, value in document.items():
        previous = before.get(source)
        if isinstance(value, dict):
            nested = changed_translations(
                value, previous if isinstance(previous, dict) else {}
            )
            if nested:
                changed[source] = nested
        elif value != previous:
            changed[source] = value
    return changed


def validate_results(
    tasks: list[Task], payload: Any, rules: dict | None = None
) -> dict[str, str]:
    """Require exactly one valid translation for every requested ID."""
    submission = Submission.model_validate(payload)
    rules = rules or {}
    expected = {task.id: task for task in tasks}
    results = {}
    for item in submission.translations:
        key = item.id
        if not isinstance(key, str) or key not in expected or key in results:
            raise ValueError(f"Unknown or duplicate result ID: {key}")
        task = expected[key]
        task_rules = combine_rules(task.rules, rules)
        validate_translation(
            task.source, item.translation, task_rules, category=task.category
        )
        results[key] = item.translation
    if set(results) != set(expected):
        raise ValueError(f"Missing {len(set(expected) - set(results))} translations")
    return results
