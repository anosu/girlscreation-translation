"""Import resources from a local JSON object, array, or directory."""

from collections.abc import Iterable

from workflow.adapters import CollectRequest
from workflow.config import StrictModel, Text
from workflow.resources import Resource
from workflow.utils import read_json


class Settings(StrictModel):
    input: Text = "sources/resources.json"


def collect(request: CollectRequest) -> Iterable[Resource]:
    options = Settings.model_validate(request.options)
    source = request.root / options.input
    if not source.exists():
        raise ValueError(
            f"Resource input not found: {source}. Add resource JSON or set adapter.input."
        )
    files = sorted(source.glob("*.json")) if source.is_dir() else [source]
    ids = set()
    for file in files:
        raw = read_json(file)
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            try:
                resource = Resource.model_validate(item)
            except ValueError as error:
                raise ValueError(f"Invalid resource in {file}: {error}") from error
            if resource.id in ids:
                raise ValueError(
                    f"Duplicate resource ID: {resource.id}; provide distinct id values for resources sharing an output/path"
                )
            ids.add(resource.id)
            if request.selection is None or resource.id in request.selection:
                yield resource
    if request.selection and set(request.selection) - ids:
        raise ValueError("Unknown selected resource IDs")
