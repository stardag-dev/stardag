"""``stardag tasks``: a completion, its instances, and the operator's
per-task actions (registry v2).

A **task** is the completion (``task_id``) with its global status and
claim; it holds no parameters. An **instance** is one construction of it
under a scope ``(deployment, settings)``, with the body (design.md, D1/D2).

    stardag tasks show <task-id>                 # task, instances, artifacts
    stardag tasks check <task-id> -m <module>    # run complete() locally
    stardag tasks retry <task-id> [--build <id>] [--yes]   # reset to PENDING
    stardag tasks cancel <task-id> [--build <id>] [--yes]  # release the claim
    stardag tasks exclude <plan-id> <task-id>    # give up on it in one plan
"""

from typing import Optional

import typer
from rich.table import Table

from stardag._cli._output import (
    JSON_OPTION,
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
from stardag.registry import TaskArtifactInfo, TaskInfo

app = typer.Typer(help="Inspect tasks and act on one task.", no_args_is_help=True)
app.command("check")(tasks_check)
app.command("retry")(tasks_retry)
app.command("cancel")(tasks_cancel)
app.command("exclude")(tasks_exclude)

_TASK_ID = typer.Argument(..., help="Task ID (the completion hash)")

# The registry serves no event read yet, so a structure divergence
# (TASK_STRUCTURE_DIVERGED: an expanded instance declared new static edges
# within its scope) cannot be shown per task. Said once, where it would be.
DIVERGENCE_NOT_SERVED = (
    "Structure divergence (TASK_STRUCTURE_DIVERGED) is recorded in the "
    "registry's event log, which it does not serve yet; it cannot be shown "
    "here."
)


# -----------------------------------------------------------------------------
# show
# -----------------------------------------------------------------------------


@app.command("show")
def tasks_show(
    task_id: str = _TASK_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show a task: its global status and claim, its instances (one per
    scope it was constructed under, newest first) and its artifacts.

    Reads ``GET /tasks/{id}`` and ``GET /tasks/{id}/artifacts``. Writes
    nothing.
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        task = registry.task_get(task_id)
        artifacts = registry.task_list_artifacts(task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(
            {
                **task.model_dump(mode="json"),
                "artifacts": [a.model_dump(mode="json") for a in artifacts],
                "structure_diverged": None,
                "notes": [DIVERGENCE_NOT_SERVED],
            }
        )
        return
    _render_task(task, artifacts)


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
    console.print(f"[dim]{DIVERGENCE_NOT_SERVED}[/dim]")
