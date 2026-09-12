"""Status, review acknowledgements and bounded cache maintenance."""

import time

from scripts.config import Project, Target
from scripts.models import read_state
from scripts.prepare import read_plan
from scripts.session import Session, group_key
from scripts.utils import digest, read_json, write_json


def status(target: Target) -> dict:
    record = {
        "target": target.code,
        "work": str(target.work),
        "review_outputs": read_state(target.state).get("review_outputs", []),
    }
    if not (target.work / "plan.json").exists():
        return {**record, "state": "not_prepared", "next": "prepare"}
    try:
        plan = read_plan(target.work)
        receipt = target.work / "publication.json"
        if receipt.exists() and read_json(receipt).get("plan") == plan.id:
            return {
                **record,
                "state": "published",
                "total": len(plan.tasks),
                "completed": len(plan.tasks),
                "remaining": 0,
                "next": "update",
            }
        if not (target.work / "session.json").exists():
            return {
                **record,
                "state": "prepared",
                "total": len(plan.tasks),
                "next": "setup",
            }
        progress = Session(target.work).status()
        results = target.work / "results.json"
        finalized = results.exists() and read_json(results).get("plan") == plan.id
        next_command = "translate"
        if not progress["remaining"]:
            next_command = "merge" if finalized else "finalize"
        return {
            **record,
            **progress,
            "state": "complete" if not progress["remaining"] else "pending",
            "next": next_command,
        }
    except (ValueError, OSError) as error:
        return {
            **record,
            "state": "needs_setup",
            "reason": str(error),
            "next": "prepare, then setup",
        }


def review(
    target: Target, acknowledged: list[str] | None = None, acknowledge_all: bool = False
) -> dict:
    original = target.state.read_bytes() if target.state.exists() else None
    state = read_state(target.state)
    files = set(state.get("review_outputs", []))
    selected = files if acknowledge_all else set(acknowledged or [])
    if selected - files:
        raise ValueError(
            f"Files are not in the review list: {sorted(selected - files)}"
        )
    if selected:
        state["review_outputs"] = sorted(files - selected)
        if target.state.read_bytes() != original:
            raise ValueError("Review state changed; retry the acknowledgement")
        write_json(target.state, state)
    return {
        "target": target.code,
        "acknowledged": sorted(selected),
        "remaining": sorted(files - selected),
    }


def prune_cache(target: Target, days: int = 30) -> dict:
    """Delete only expired inactive artifacts; never touch a current task or publication."""
    if days <= 0:
        raise ValueError("Cache retention must be greater than zero days")
    plan = read_plan(target.work) if (target.work / "plan.json").exists() else None
    active_answers = {task.id for task in plan.tasks} if plan else set()
    active_groups = (
        {digest(group_key(task))[:20] for task in plan.tasks} if plan else set()
    )
    cutoff = time.time() - days * 86400
    removed = []
    for directory in ("answers", "groups", "proposals"):
        for path in (target.work / directory).glob("*.json"):
            if directory == "answers" and path.stem in active_answers:
                continue
            if directory == "groups" and path.stem in active_groups:
                continue
            if directory == "proposals" and plan and path.stem == plan.id:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(
                target.work.resolve()
            ):
                raise ValueError(f"Cache entry escapes work directory: {path}")
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.relative_to(target.work).as_posix())
    return {"target": target.code, "retention_days": days, "removed": removed}


def run_summary(project: Project, targets: list[Target]) -> str:
    lines = [
        f"# Translation update: {project.name}",
        "",
        "| Target | Selected / available | Reuse candidates | Completed | Remaining | Blocked entries | Review files |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for target in targets:
        report_path = target.work / "prepare-report.json"
        report = read_json(report_path) if report_path.exists() else {}
        progress = status(target)
        review_files = (
            progress["review_outputs"]
            if progress["state"] == "published"
            else report.get("review_outputs", progress["review_outputs"])
        )
        lines.append(
            f"| {target.code} | {report.get('tasks', 0)} / {report.get('available_tasks', 0)} | {report.get('reuse_candidates', 0)} | {progress.get('completed', 0)} | {progress.get('remaining', '?')} | {report.get('blocked_entries', 0)} | {len(review_files)} |"
        )
        if report.get("existing_variants"):
            lines.extend(
                [
                    "",
                    f"{target.code}: {len(report['existing_variants'])} source entries have different existing translations; preserved and listed in prepare-report.json.",
                    "",
                ]
            )
        if progress.get("reason"):
            lines.extend(["", f"{target.code}: {progress['reason']}"])
    lines.extend(
        [
            "",
            "Task completion and structural checks do not certify translation quality. No monetary cost is inferred from task counts.",
            "",
        ]
    )
    return "\n".join(lines)
