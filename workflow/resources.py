"""Compact source materials; only these models cross the adapter boundary."""

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from workflow.config import Rules, StrictModel, Text
from workflow.dictionaries import namespace_path, output_path


class TextBlock(StrictModel):
    texts: list[str]
    context: dict | list = Field(default_factory=dict)

    @field_validator("texts")
    @classmethod
    def nonblank(cls, values):
        if any(not text.strip() for text in values):
            raise ValueError("Source text must not be blank")
        return values


class Resource(StrictModel):
    id: Text = ""
    output: str
    path: list[str] = Field(default_factory=list)
    kind: Literal["dialogue", "text"]
    lines: list[list[str | None]] = Field(default_factory=list)
    blocks: list[TextBlock] = Field(default_factory=list)
    context: dict | list = Field(default_factory=dict)
    rules: dict = Field(default_factory=dict)
    term: bool = False

    _output = field_validator("output")(output_path)
    _path = field_validator("path")(namespace_path)

    @model_validator(mode="after")
    def valid_material(self):
        if not self.id:
            suffix = (
                "#" + json.dumps(self.path, ensure_ascii=False, separators=(",", ":"))
                if self.path
                else ""
            )
            self.id = self.output + suffix
        Rules.model_validate(self.rules)
        if self.kind == "dialogue":
            if self.blocks:
                raise ValueError("Dialogue resources use lines, not blocks")
            for line in self.lines:
                if (
                    len(line) != 2
                    or not isinstance(line[1], str)
                    or not line[1].strip()
                ):
                    raise ValueError(
                        "Dialogue lines must be [speaker or null, nonblank source]"
                    )
        elif self.lines:
            raise ValueError("Text resources use blocks, not lines")
        return self

    def occurrences(self) -> list[dict]:
        """Preserve reading order and repetition; numbers are local to this snapshot."""
        if self.kind == "dialogue":
            return [
                {"line": i, "speaker": speaker, "source": source}
                for i, (speaker, source) in enumerate(self.lines, 1)
            ]
        return [
            {"block": b, "line": i, "source": text}
            for b, block in enumerate(self.blocks, 1)
            for i, text in enumerate(block.texts, 1)
        ]


class Snapshot(StrictModel):
    version: Literal[1] = 1
    project: Text
    source_language: Text
    resources: dict[Text, str]

    @field_validator("resources")
    @classmethod
    def safe_files(cls, values):
        for value in values.values():
            output_path(value)
        return values
