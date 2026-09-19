"""Publish immutable local snapshots only after collection succeeds."""

import io
import zipfile
from pathlib import Path

from workflow.adapters import CollectRequest, load_adapter
from workflow.config import Project, Target
from workflow.resources import Resource, Snapshot, output_path
from workflow.utils import digest, directory_digest, read_json, write_bytes, write_json


def read_resource(root: Path, file: str) -> Resource:
    output_path(file)
    path = (root / file).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Resource path escapes snapshot")
    resource = Resource.model_validate(read_json(path))
    if path.stem != digest(resource.model_dump(exclude_defaults=True)):
        raise ValueError("Source snapshot changed")
    return resource


def read_snapshot(project: Project) -> tuple[Snapshot, list[str], str]:
    if not (project.sources / "index.json").exists():
        raise ValueError(
            f'No local snapshot. Run sync first: npm run workflow -- sync --config "{project.config}"'
        )
    pointer = read_json(project.sources / "index.json")["snapshot"]
    output_path(pointer)
    path = (project.sources / pointer).resolve()
    if not path.is_relative_to(project.sources.resolve()):
        raise ValueError("Snapshot path escapes source directory")
    snapshot = Snapshot.model_validate(read_json(path))
    if path.stem != digest(snapshot.model_dump()):
        raise ValueError("Source snapshot changed")
    if (
        snapshot.project != project.id
        or snapshot.source_language != project.source_language
    ):
        raise ValueError("Snapshot belongs to another project or source language")
    for key, file in snapshot.resources.items():
        if read_resource(project.sources, file).id != key:
            raise ValueError("Snapshot resource ID mismatch")
    files = [pointer, *sorted(set(snapshot.resources.values()))]
    return snapshot, files, directory_digest(project.sources, files)


def sync_sources(
    project: Project,
    selection: list[str] | None = None,
    *,
    targets: list[Target] | None = None,
    export: bool = False,
) -> Snapshot:
    request = CollectRequest(
        project.root,
        project.options,
        project.sources.parent / "adapter-cache",
        selection,
        {
            target.code: target.translations
            for target in (targets if targets is not None else project.select())
        },
    )
    resources = {}
    # Failed runs may leave unused objects, but never replace the last valid index.
    for item in load_adapter(project.adapter).collect(request):
        if not isinstance(item, Resource):
            raise ValueError("collect(request) must yield Resource instances")
        resource = Resource.model_validate(item.model_dump())
        if resource.id in resources:
            raise ValueError(f"Duplicate resource ID: {resource.id}")
        data = resource.model_dump(exclude_defaults=True)
        file = f"objects/{digest(data)}.json"
        destination = (project.sources / file).resolve()
        if not destination.is_relative_to(project.sources.resolve()):
            raise ValueError("Source destination escapes cache")
        write_json(destination, data)
        resources[resource.id] = file
    snapshot = Snapshot(
        project=project.id, source_language=project.source_language, resources=resources
    )
    file = f"snapshots/{digest(snapshot.model_dump())}.json"
    destination = (project.sources / file).resolve()
    if not destination.is_relative_to(project.sources.resolve()):
        raise ValueError("Snapshot destination escapes cache")
    write_json(destination, snapshot.model_dump())
    write_json(project.sources / "index.json", {"snapshot": file})
    if export:
        _, files, _ = read_snapshot(project)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in ["index.json", *files]:
                bundle.write(project.sources / name, name)
        write_bytes(project.source_bundle, archive.getvalue())
    return snapshot
