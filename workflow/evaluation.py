"""Offline quality checks; reference similarity is reported separately from hard rules."""

from pathlib import Path
from typing import Literal

from pydantic import Field

from workflow.config import Locale, Rules, StrictModel, Text
from workflow.utils import read_json, write_json
from workflow.validate import validate_translation


class QualityCase(StrictModel):
    id: Text
    source: Text
    reference: Text
    review_status: Literal["confirmed", "pending"] = "pending"
    note: str = ""
    rules: Rules = Field(default_factory=Rules)


class QualitySuite(StrictModel):
    source_language: Locale
    target: Locale
    cases: list[QualityCase] = Field(min_length=1)


def evaluate(suite_path: Path, answers_path: Path, output: Path | None = None) -> dict:
    suite = QualitySuite.model_validate(read_json(suite_path))
    answers = read_json(answers_path)
    ids = [case.id for case in suite.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Quality case IDs must be unique")
    if not isinstance(answers, dict) or set(answers) - set(ids):
        raise ValueError(
            "Answers must map quality case IDs to translations, without unknown IDs"
        )
    records = []
    for case in suite.cases:
        answer = answers.get(case.id)
        error = None
        try:
            validate_translation(
                case.source, answer, case.rules.model_dump(exclude_none=True)
            )
        except ValueError as failure:
            error = str(failure)
        records.append(
            {
                "id": case.id,
                "source": case.source,
                "translation": answer,
                "reference": case.reference,
                "note": case.note,
                "review_status": case.review_status,
                "reference_match": answer == case.reference,
                "error": error,
                "needs_human_review": error is None
                and (case.review_status == "pending" or answer != case.reference),
            }
        )
    report = {
        "source_language": suite.source_language,
        "target": suite.target,
        "cases": len(records),
        "confirmed_references": sum(
            case.review_status == "confirmed" for case in suite.cases
        ),
        "errors": sum(record["error"] is not None for record in records),
        "reference_matches": sum(record["reference_match"] for record in records),
        "results": records,
        "note": "Reference matches are not a semantic quality score. No model calls were made.",
    }
    if output:
        write_json(output, report)
    return report
