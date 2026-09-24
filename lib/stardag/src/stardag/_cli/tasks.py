"""``stardag tasks``: a completion, its instances, and the operator's
per-task actions (registry v2).

A **task** is the completion (``task_id``) with its global status and
claim; it holds no parameters. An **instance** is one construction of it
under a scope ``(deployment, settings)``, with the body (design.md, D1/D2).

    stardag tasks list [--status S] [--cursor C]  # most recent change first
    stardag tasks show <task-id>                 # task, claim, executions, events
    stardag tasks check <task-id> -m <module>    # run complete() locally
    stardag tasks retry <task-id> [--build <id>] [--yes]   # reset to PENDING
    stardag tasks cancel <task-id> [--build <id>] [--yes]  # release the claim
    stardag tasks exclude <plan-id> <task-id>    # give up on it in one plan
"""

import json
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.table import Table

from stardag._cli._output import (
    JSON_OPTION,
    as_utc,
    emit_json,
    short,
    stamp,
)
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
)
from stardag._cli.tasks_actions import (
    tasks_cancel,
    tasks_check,
    tasks_exclude,
    tasks_retry,
)
from stardag.exceptions import StardagError
from stardag._cli.executions import render_executions
from stardag.registry import EventInfo, TaskArtifactInfo, TaskInfo

app = typer.Typer(help="Inspect tasks and act on one task.", no_args_is_help=True)
app.command("check")(tasks_check)
app.command("retry")(tasks_retry)
app.command("cancel")(tasks_cancel)
app.command("exclude")(tasks_exclude)

_TASK_ID = typer.Argument(..., help="Task ID (the completion hash)")

# How ``tasks show`` reads the event log: the server serves at most 500 of
# a task's events, oldest first.
EVENT_READ_LIMIT = 500
# The wire value of EventType.TASK_STRUCTURE_DIVERGED (the server serves
# event types lowercase).
STRUCTURE_DIVERGED = "task_structure_diverged"


# -----------------------------------------------------------------------------
# show
# -----------------------------------------------------------------------------


@app.command("show")
def tasks_show(
    task_id: str = _TASK_ID,
    include_ended: bool = typer.Option(
        False,
        "--include-ended",
        help="List every execution of the task, not only those with no end reported.",
    ),
    events: int = typer.Option(
        10, "--events", min=0, max=EVENT_READ_LIMIT, help="Last events to show."
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show a task: its global status and the claim's holder (plan and
    build), its instances (one per scope it was constructed under, newest
    first), its executions across builds, its last events — every
    structure divergence (``TASK_STRUCTURE_DIVERGED``: an expanded instance
    declared new static edges within its scope) called out — and its
    artifacts.

    Reads ``GET /tasks/{id}``, ``GET /tasks/{id}/executions``,
    ``GET /tasks/{id}/events`` (at most the task's first 500) and
    ``GET /tasks/{id}/artifacts``. Writes nothing.
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        task = registry.task_get(task_id)
        executions = registry.task_list_executions(task_id, include_ended=include_ended)
        log = registry.task_events(task_id, limit=EVENT_READ_LIMIT)
        artifacts = registry.task_list_artifacts(task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    diverged = [e for e in log if e.event_type.lower() == STRUCTURE_DIVERGED]
    if json_output:
        emit_json(
            {
                **task.model_dump(mode="json"),
                "executions": [x.model_dump(mode="json") for x in executions],
                # --events bounds the listing here too; divergences are
                # reported whole below, as in the text output.
                "events": [e.model_dump(mode="json") for e in log[-events:]]
                if events
                else [],
                "structure_diverged": [e.model_dump(mode="json") for e in diverged],
                "artifacts": [a.model_dump(mode="json") for a in artifacts],
            }
        )
        return
    _render_task(task, artifacts)
    render_executions(
        executions,
        title=f"{'Executions' if include_ended else 'Unended executions'} "
        f"({len(executions)}, newest first)",
        by_task=True,
    )
    _render_events(log, diverged, events)


def _render_events(log: list[EventInfo], diverged: list[EventInfo], last: int) -> None:
    if diverged:
        console.print(
            f"[bold yellow]Structure diverged {len(diverged)} time(s)[/bold yellow] "
            "(an expanded instance declared new static edges within its scope; "
            "appended, never refused):"
        )
        for e in diverged:
            detail = (
                json.dumps(e.event_metadata, sort_keys=True) if e.event_metadata else ""
            )
            console.print(
                f"  {stamp(e.created_at)} plan {short(e.plan_id, 8)} {detail}"
            )
    if not last or not log:
        return
    table = Table(title=f"Last events ({min(last, len(log))} of {len(log)})")
    for col in ("When", "Event", "Build", "Execution", "Applied", "Detail"):
        table.add_column(col)
    for e in log[-last:]:
        table.add_row(
            stamp(e.created_at),
            e.event_type,
            short(e.build_id, 8),
            short(e.execution_id, 8),
            "yes" if e.report_applied else "no (late)",
            e.error_message or "",
        )
    console.print(table)


def _claim(task: TaskInfo) -> str:
    if task.claim_build_id is None:
        return "-"
    live = task.claim_expires_at is not None and as_utc(
        task.claim_expires_at
    ) > datetime.now(timezone.utc)
    state = "live" if live else "lapsed"
    return f"build {task.claim_build_id}, plan {task.claim_plan_id} ({state})"


def _render_task(task: TaskInfo, artifacts: list[TaskArtifactInfo]) -> None:
    name = f"{task.task_namespace}.{task.task_name}".lstrip(".")
    table = Table(title=f"Task {task.task_id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Task", name or "-")
    table.add_row("Version", task.version or "-")
    table.add_row("Output", task.output_uri or "-")
    table.add_row("Status", f"{task.status or '-'} (since {stamp(task.status_at)})")
    table.add_row("Completed", stamp(task.completed_at))
    if task.error_message:
        table.add_row("Error", task.error_message)
    table.add_row("Claim held by", _claim(task))
    if task.execution_id is not None:
        table.add_row("Execution", str(task.execution_id))
    if task.claim_expires_at is not None:
        table.add_row("Claim expires", stamp(task.claim_expires_at))
    console.print(table)
    instances = Table(title=f"Instances ({len(task.instances)}, newest first)")
    for col in ("Instance", "Deployment", "Settings", "Expanded", "Created"):
        instances.add_column(col)
    for i in task.instances:
        instances.add_row(
            str(i.id),
            str(i.deployment_id),
            short(i.settings_hash),
            stamp(i.expanded_at) if i.expanded_at else "no",
            stamp(i.created_at),
        )
    console.print(instances)
    if artifacts:
        rows = Table(title="Artifacts")
        for col in ("Type", "Name", "Created"):
            rows.add_column(col)
        for a in artifacts:
            rows.add_row(a.artifact_type, a.name, stamp(a.created_at))
        console.print(rows)


# -----------------------------------------------------------------------------
# list
# -----------------------------------------------------------------------------


@app.command("list")
def tasks_list(
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Only tasks in this global status (pending, running, completed, "
        "failed, cancelled, skipped, suspended, interrupted).",
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
    cursor: Optional[str] = typer.Option(
        None,
        "--cursor",
        help="Start after the previous page (the cursor it printed as next).",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List tasks in the environment, most recent status change first, a
    page at a time. ``--status running`` is the claim-holder question: the
    Claim column names the build holding each claim.

    Reads ``GET /tasks``. Writes nothing. v1's ``--older-than``, ``--name``
    and ``--namespace`` filters are not available: the server's task list
    filters by status only.
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        page = registry.task_list(status=status, limit=limit, cursor=cursor)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        # List items carry no instances (``GET /tasks`` serves none).
        emit_json(
            page.model_dump(mode="json", exclude={"tasks": {"__all__": {"instances"}}})
        )
        return
    if not page.tasks:
        console.print("No tasks match.")
        return
    table = Table(title="Tasks (most recent status change first)")
    for col in ("Task ID", "Task", "Status", "Since", "Claim (build)"):
        table.add_column(col)
    for t in page.tasks:
        table.add_row(
            t.task_id,
            f"{t.task_namespace}.{t.task_name}".lstrip("."),
            t.status or "-",
            stamp(t.status_at),
            str(t.claim_build_id) if t.claim_build_id else "-",
        )
    console.print(table)
    if page.next_cursor:
        console.print(f"[dim]Next page: --cursor {page.next_cursor}[/dim]")
