"""``stardag tasks``: a completion, its instances, and the operator's
per-task actions (registry v2).

A **task** is the completion (``task_id``) with its global status and
claim; it holds no parameters. An **instance** is one construction of it
under a scope ``(deployment, settings)``, with the body (design.md, D1/D2).

    stardag tasks show <task-id>                 # task, instances, artifacts
    stardag tasks check <task-id> -m <module>    # run complete() locally
    stardag tasks retry <task-id> --build <id>   # reset to PENDING
    stardag tasks cancel <task-id> --build <id>  # release the build's claim
    stardag tasks exclude <plan-id> <task-id>    # give up on it in one plan
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import typer
from rich.table import Table

from stardag._cli._output import (
    JSON_OPTION,
    YES_OPTION,
    emit_json,
    parse_uuid,
    short,
    stamp,
)
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError
from stardag.registry import TaskArtifactInfo, TaskInfo

app = typer.Typer(help="Inspect tasks and act on one task.", no_args_is_help=True)

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


# -----------------------------------------------------------------------------
# check
# -----------------------------------------------------------------------------


def _import_modules(modules: list[str]) -> None:
    import importlib

    from stardag.build._task_modules import expand_task_module_patterns

    for module in expand_task_module_patterns(modules):
        try:
            importlib.import_module(module)
        except Exception as e:
            error_console.print(
                f"[bold red]Error:[/bold red] could not import {module!r}: "
                f"{type(e).__name__}: {e}"
            )
            raise typer.Exit(1)


@app.command("check")
def tasks_check(
    task_id: str = _TASK_ID,
    module: list[str] = typer.Option(
        ...,
        "--module",
        "-m",
        help="Import path of a module defining the task's class (repeatable; "
        "task-module patterns like 'pkg.tasks.*' are expanded).",
    ),
    report: bool = typer.Option(
        False,
        "--report",
        help="Refused: this command prints the observation; trigger a build "
        "to let the registry observe the task.",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Check a task's target from here: rehydrate its newest instance, run
    ``complete()`` locally, and print the observation next to the
    registry's status.

    Reads ``GET /tasks/{id}``; reads the task's target (this process's
    target configuration). Writes nothing: it prints; trigger a build to
    let the registry observe. The registry follows the world (D7): a task
    whose target is gone is invalidated by the next build that observes
    it, and there is no operator path to an invalidation, so ``--report``
    is refused.
    """
    if report:
        error_console.print(
            "[bold red]Error:[/bold red] --report is refused: tasks check "
            "prints; trigger a build to let the registry observe. There is no "
            "operator path to an invalidation (D7)."
        )
        raise typer.Exit(1)
    _import_modules(module)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        info = registry.task_get(task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if not info.instances:
        error_console.print(
            f"[bold red]Error:[/bold red] the registry holds no instance of "
            f"task {task_id} to rehydrate."
        )
        raise typer.Exit(1)
    from stardag._core.rehydrate import TaskRehydrationError, task_from_registry_data

    instance = info.instances[0]
    try:
        task = task_from_registry_data(instance.body, expected_task_id=task_id)
    except TaskRehydrationError as e:
        error_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
    observed_complete = task.complete()
    observed_at = datetime.now(timezone.utc)
    registry_complete = info.status == "completed"
    payload: dict[str, Any] = {
        "task_id": task_id,
        "instance_id": str(instance.id),
        "observed_complete": observed_complete,
        "observed_at": observed_at.isoformat(),
        "registry_status": info.status,
        "agrees": observed_complete == registry_complete,
        "reported": False,
    }
    if json_output:
        emit_json(payload)
        return
    verdict = "[green]complete[/green]" if observed_complete else "[red]missing[/red]"
    console.print(
        f"Task {task_id}: target {verdict} (observed {stamp(observed_at)}); "
        f"registry status: {info.status}."
    )
    if registry_complete and not observed_complete:
        console.print(
            "[yellow]The registry records a completion whose target is gone."
            "[/yellow] The next build that includes this task observes it and "
            "invalidates the completion (TASK_INVALIDATED), then re-runs it."
        )
    elif observed_complete and not registry_complete:
        console.print(
            "[dim]The target exists but the registry does not record the "
            "completion; the next build that includes the task records it."
            "[/dim]"
        )
    console.print("[dim]Nothing was reported to the registry.[/dim]")


# -----------------------------------------------------------------------------
# retry / cancel / exclude
# -----------------------------------------------------------------------------


def _active_plan_of(registry, build_id: UUID) -> UUID:
    frontier = registry.build_get_frontier(build_id)
    if frontier.plan_id is None:
        error_console.print(
            f"[bold red]Error:[/bold red] build {build_id} has no active plan."
        )
        raise typer.Exit(1)
    return frontier.plan_id


def _member_action(
    action: str,
    task_id: str,
    build: str,
    stardag_profile: Optional[str],
    stardag_env: Optional[str],
    json_output: bool,
) -> None:
    build_id = parse_uuid(build, "build ID")
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        plan_id = _active_plan_of(registry, build_id)
        if action == "retry":
            result = registry.member_retry(plan_id, task_id)
        else:
            result = registry.member_cancel(plan_id, task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json({"plan_id": str(plan_id), **result.model_dump(mode="json")})
        return
    changed = "" if result.applied else " (no change)"
    console.print(f"Task {task_id} is {result.status}{changed}.")


_BUILD_OPTION = typer.Option(
    ..., "--build", "-b", help="The build whose active plan holds the task."
)


@app.command("retry")
def tasks_retry(
    task_id: str = _TASK_ID,
    build: str = _BUILD_OPTION,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Reset a failed (or otherwise ended) task to PENDING, so the build's
    next tick or driver runs it again.

    Reads ``GET /builds/{id}/frontier`` (the active plan); writes
    ``POST /plans/{plan}/members/{task}/retry``. Refused on COMPLETED and
    under a live claim; a PENDING task is a no-op. The global status
    changes, so every build holding the task sees it.
    """
    _member_action("retry", task_id, build, stardag_profile, stardag_env, json_output)


@app.command("cancel")
def tasks_cancel(
    task_id: str = _TASK_ID,
    build: str = _BUILD_OPTION,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Cancel one task: release the claim the build holds on it (CANCELLED,
    actionable). Nothing is stopped — the execution exits at its next
    cooperative checkpoint; ``builds stop --task-id`` stops it.

    Reads ``GET /builds/{id}/frontier``; writes
    ``POST /plans/{plan}/members/{task}/cancel``, refused (409
    ``not_claim_holder``) unless the build holds the task's claim.
    """
    _member_action("cancel", task_id, build, stardag_profile, stardag_env, json_output)


@app.command("exclude")
def tasks_exclude(
    plan_id: str = typer.Argument(..., help="Plan ID"),
    task_id: str = _TASK_ID,
    reason: str = typer.Option(..., "--reason", help="Why (recorded on the event)."),
    yes: bool = YES_OPTION,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Give up on a task in one plan: exclude it and its downstream closure
    (short of COMPLETED members). An excluded root fails the build.

    Writes ``POST /plans/{plan}/members/{task}/exclude``. The task's global
    status is untouched; other builds are unaffected. Refused on a
    superseded plan.
    """
    parsed = parse_uuid(plan_id, "plan ID")
    if not yes:
        if json_output:
            error_console.print(
                "[bold red]Error:[/bold red] refusing to prompt in --json mode; "
                "pass --yes to confirm."
            )
            raise typer.Exit(1)
        typer.confirm(
            f"Exclude task {task_id} and its downstream from plan {plan_id}?",
            abort=True,
        )
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.member_exclude(parsed, task_id, reason=reason)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(result.model_dump(mode="json"))
        return
    console.print(f"Excluded {len(result.excluded)} member(s) from plan {plan_id}:")
    for excluded in result.excluded:
        console.print(f"  {excluded}")
    if result.roots_excluded:
        roots = ", ".join(result.roots_excluded)
        if result.build_failed:
            console.print(
                f"[bold red]This exclusion reached root(s) {roots}: the build "
                "failed.[/bold red]"
            )
        else:
            console.print(
                f"[bold yellow]This exclusion reached root(s) {roots}; the "
                "build was already terminal and is unchanged.[/bold yellow]"
            )
