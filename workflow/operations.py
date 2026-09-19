"""Task status and bounded cache maintenance."""

import time

from workflow.config import Project, Target
from workflow.prepare import read_plan
from workflow.session import Session
from workflow.utils import read_json


def status(target: Target) -> dict:
    record = {
        "target": target.code,
        "work": str(target.work),
    }
    if not (target.work / "plan.json").exists():
        return {**record, "state": "not_prepared", "next": "sync, then plan"}
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
                "next": "translate",
            }
        progress = Session(target.work).status()
        results = target.work / "results.json"
        finalized = results.exists() and read_json(results).get("plan") == plan.id
        next_command = "translate"
        if not progress["remaining"]:
            next_command = "publish" if finalized else "translate"
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
            "next": "plan, then translate",
        }


def prune_cache(target: Target, days: int = 30) -> dict:
    """Delete only expired inactive artifacts; never touch a current task or publication."""
    if days <= 0:
        raise ValueError("Cache retention must be greater than zero days")
    plan = read_plan(target.work) if (target.work / "plan.json").exists() else None
    active_answers = {task.id for task in plan.tasks} if plan else set()
    cutoff = time.time() - days * 86400
    removed = []
    for directory in ("answers", "proposals"):
        for path in (target.work / directory).glob("*.json"):
            if directory == "answers" and path.stem in active_answers:
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
        "| Target | Resources selected / available | Dictionary keys selected / available | Reuse candidates | Completed | Remaining in plan | Deferred by limit |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for target in targets:
        report_path = target.work / "prepare-report.json"
        report = read_json(report_path) if report_path.exists() else {}
        progress = status(target)
        lines.append(
            f"| {target.code} | {report.get('resources', 0)} / {report.get('available_resources', 0)} | {report.get('tasks', 0)} / {report.get('available_tasks', report.get('tasks', 0))} | {report.get('reuse_candidates', 0)} | {progress.get('completed', 0)} | {progress.get('remaining', '?')} | {report.get('deferred_tasks', 0)} |"
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
        if "packets" in report:
            lines.extend(
                [
                    "",
                    f"{target.code}: {report['packets']} work packets for {report.get('resource_packets', 0)} resources with tasks; only packets still missing answers start an agent.",
                ]
            )
    lines.extend(
        [
            "",
            "Task completion and structural checks do not certify translation quality. No monetary cost is inferred from task counts.",
            "",
        ]
    )
    return "\n".join(lines)
