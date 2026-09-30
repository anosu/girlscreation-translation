"""Build bounded translation windows from complete, context-bearing units."""

from collections.abc import Iterable

from workflow.models import Task
from workflow.resources import Resource

# These are source-material budgets, not model-token limits. A unit (a scene or
# a table window) is never split merely to hit a window boundary.
WINDOW_ITEMS = 480
WINDOW_CHARS = 32000
MAX_STORIES = 6


def assign_windows(
    tasks: list[Task], resources: list[Resource], files: dict[str, str]
) -> dict[str, list[str]]:
    """Assign tasks while keeping complete scenes and table windows together.

    A dialogue resource and all auxiliary resources for the same output file
    form one indivisible unit. Several such units may share a window. Text
    resources are windowed by output file and parent path, then split only when
    one window itself exceeds the source-material budget.
    """
    windows: dict[str, list[str]] = {}
    by_id = {resource.id: resource for resource in resources}
    by_file = {file: key for key, file in files.items()}
    scenes = {resource.output for resource in resources if resource.kind == "dialogue"}

    def task_size(batch: Iterable[Task]) -> tuple[int, int]:
        batch = list(batch)
        return len(batch), sum(len(task.source) for task in batch)

    def add(batch: list[Task]) -> None:
        if not batch:
            return
        members = dict.fromkeys(
            by_file[reference] for task in batch for reference in task.references
        )
        # A title may be the only pending resource while its completed dialogue
        # is still required as context.
        for output in {task.output for task in batch if task.output in scenes}:
            for resource in resources:
                if resource.output == output:
                    members[resource.id] = None
        ordered = sorted(members, key=lambda key: by_id[key].kind != "dialogue")
        window = f"window-{len(windows) + 1}"
        windows[window] = ordered
        for task in batch:
            task.window = window

    def pack_units(units: list[list[Task]], *, max_units: int | None = None) -> None:
        batch: list[Task] = []
        items = chars = unit_count = 0
        for unit in units:
            unit_items, unit_chars = task_size(unit)
            # Keep a table together when it fits; split a large field by task only.
            if batch and (
                items + unit_items > WINDOW_ITEMS
                or chars + unit_chars > WINDOW_CHARS
                or (max_units is not None and unit_count >= max_units)
            ):
                add(batch)
                batch, items, chars, unit_count = [], 0, 0, 0
            # A single scene or table window remains whole even if it exceeds
            # the advisory window budget.
            batch.extend(unit)
            items += unit_items
            chars += unit_chars
            unit_count += 1
        add(batch)

    terms: dict[str, list[Task]] = {}
    stories: dict[str, list[Task]] = {}
    texts: dict[tuple[str, tuple[str, ...]], list[Task]] = {}
    for task in tasks:
        if task.term:
            terms.setdefault(task.window, []).append(task)
        elif task.output in scenes:
            stories.setdefault(task.output, []).append(task)
        else:
            texts.setdefault((task.output, tuple(task.path[:-1])), []).append(task)
    # Process standard names first so later windows see accepted terminology.
    pack_units(list(terms.values()))

    # A story is the context unit. Titles and other resources sharing its output
    # stay with it, while neighboring short stories can share one session.
    story_units = []
    for output, story_tasks in stories.items():
        story_units.append(
            story_tasks
            + [
                task
                for task in tasks
                if task.output == output and not task.term and task not in story_tasks
            ]
        )
    pack_units(story_units, max_units=MAX_STORIES)

    # Keep fields from the same table together when possible. Oversized fields
    # are the only text windows split at individual task boundaries.
    text_units: list[list[Task]] = []
    for window in texts.values():
        if (
            task_size(window)[0] <= WINDOW_ITEMS
            and task_size(window)[1] <= WINDOW_CHARS
        ):
            text_units.append(window)
        else:
            text_units.extend([task] for task in window)
    pack_units(text_units)
    return windows
