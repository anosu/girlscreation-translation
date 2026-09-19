"""Group translation work by context, with bounded batches for short texts."""

from workflow.models import Task
from workflow.resources import Resource

# Bounds on source material, not a token estimate or a limit on whole scenes.
TEXT_ITEMS = 120
TEXT_CHARS = 12000


def assign_packets(
    tasks: list[Task], resources: list[Resource], files: dict[str, str]
) -> dict[str, list[str]]:
    """Names first, intact scenes with their titles, then related text batches.

    Mutates only Task.group. Task identities and destination dictionaries stay intact.
    Completed materials remain in the plan for optional searches and reading.
    """
    packets: dict[str, list[str]] = {}
    by_id = {resource.id: resource for resource in resources}
    by_file = {file: key for key, file in files.items()}
    scenes = {resource.output for resource in resources if resource.kind == "dialogue"}

    def add(batch: list[Task], scene: str | None = None) -> None:
        if not batch:
            return
        members = dict.fromkeys(
            by_file[reference] for task in batch for reference in task.references
        )
        if scene is not None:
            # A missing title still needs its already-translated dialogue as context.
            members = dict.fromkeys(r.id for r in resources if r.output == scene)
        ordered = sorted(members, key=lambda key: by_id[key].kind != "dialogue")
        group = f"packet-{len(packets) + 1}"
        packets[group] = ordered
        for task in batch:
            task.group = group

    def bounded(groups: list[list[Task]]) -> None:
        batch: list[Task] = []
        chars = 0
        for group in groups:
            size = sum(len(task.source) for task in group)
            # Keep a table together when it fits; split a large field by task only.
            if batch and (
                len(batch) + len(group) > TEXT_ITEMS or chars + size > TEXT_CHARS
            ):
                add(batch)
                batch, chars = [], 0
            for task in group:
                if batch and (
                    len(batch) >= TEXT_ITEMS or chars + len(task.source) > TEXT_CHARS
                ):
                    add(batch)
                    batch, chars = [], 0
                batch.append(task)
                chars += len(task.source)
        add(batch)

    terms: dict[str, list[Task]] = {}
    stories: dict[str, list[Task]] = {}
    texts: dict[tuple[str, tuple[str, ...]], list[Task]] = {}
    for task in tasks:
        if task.term:
            terms.setdefault(task.group, []).append(task)
        elif task.output in scenes:
            stories.setdefault(task.output, []).append(task)
        else:
            texts.setdefault((task.output, tuple(task.path[:-1])), []).append(task)
    bounded(list(terms.values()))
    for scene, batch in stories.items():
        add(batch, scene)
    bounded(list(texts.values()))
    return packets
