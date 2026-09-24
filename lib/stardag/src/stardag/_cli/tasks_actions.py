"""``stardag tasks check/retry/cancel/exclude``: the operator's per-task
actions (registry v2). Split from :mod:`stardag._cli.tasks` by the
module-size rule; the commands are registered on its ``app``.
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import typer

from stardag._cli._output import (
    JSON_OPTION,
    YES_OPTION,
    emit_json,
    parse_uuid,
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

_TASK_ID = typer.Argument(..., help="Task ID (the completion hash)")


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


def _build_of(registry, task_id: str, build: Optional[str]) -> UUID:
    """``--build`` when given; otherwise the build holding the task's claim
    (``claim_build_id`` from ``GET /tasks/{id}``)."""
    if build is not None:
        return parse_uuid(build, "build ID")
    holder = registry.task_get(task_id).claim_build_id
    if holder is None:
        error_console.print(
            f"[bold red]Error:[/bold red] task {task_id} holds no claim, so "
            "there is no build to default to; pass --build <build-id> (a "
            "build whose active plan holds the task)."
        )
        raise typer.Exit(1)
    return holder


_PROMPTS = {
    "retry": "Reset task {task} to PENDING through build {build}? Its global "
    "status changes, so every build holding it sees it.",
    "cancel": "Cancel task {task} in build {build}? This releases the claim "
    "the build holds on it and any limit slots; the execution itself is not "
    "stopped.",
}


def _member_action(
    action: str,
    task_id: str,
    build: Optional[str],
    yes: bool,
    stardag_profile: Optional[str],
    stardag_env: Optional[str],
    json_output: bool,
) -> None:
    if build is not None:
        parse_uuid(build, "build ID")
    if not yes and json_output:
        error_console.print(
            "[bold red]Error:[/bold red] refusing to prompt in --json mode; "
            "pass --yes to confirm."
        )
        raise typer.Exit(1)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        build_id = _build_of(registry, task_id, build)
        if not yes:
            typer.confirm(
                _PROMPTS[action].format(task=task_id, build=build_id), abort=True
            )
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
        emit_json(
            {
                "build_id": str(build_id),
                "plan_id": str(plan_id),
                **result.model_dump(mode="json"),
            }
        )
        return
    changed = "" if result.applied else " (no change)"
    console.print(f"Task {task_id} is {result.status}{changed} (build {build_id}).")


_BUILD_OPTION = typer.Option(
    None,
    "--build",
    "-b",
    help="The build whose active plan holds the task. Defaults to the build "
    "holding the task's claim (claim_build_id from GET /tasks/{id}); "
    "required when the task holds none.",
)


def tasks_retry(
    task_id: str = _TASK_ID,
    build: Optional[str] = _BUILD_OPTION,
    yes: bool = YES_OPTION,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Reset a failed (or otherwise ended) task to PENDING, so the build's
    next tick or driver runs it again. Asks for confirmation unless
    ``--yes``.

    ``--build`` defaults to the build holding the task's claim (a task
    left RUNNING under a lapsed claim); a FAILED task holds none, so name
    the build. Reads ``GET /tasks/{id}`` (without ``--build``) and ``GET
    /builds/{id}/frontier`` (the active plan); writes ``POST
    /plans/{plan}/members/{task}/retry``. Refused on COMPLETED and under a
    live claim; a PENDING task is a no-op. The global status changes, so
    every build holding the task sees it.
    """
    _member_action(
        "retry", task_id, build, yes, stardag_profile, stardag_env, json_output
    )


def tasks_cancel(
    task_id: str = _TASK_ID,
    build: Optional[str] = _BUILD_OPTION,
    yes: bool = YES_OPTION,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Cancel one task: release the claim the build holds on it (CANCELLED,
    actionable). Nothing is stopped — the execution exits at its next
    cooperative checkpoint; ``builds stop --task-id`` stops it. Asks for
    confirmation unless ``--yes``.

    ``--build`` defaults to the claim's holder (``claim_build_id`` from
    ``GET /tasks/{id}``). Reads ``GET /builds/{id}/frontier``; writes
    ``POST /plans/{plan}/members/{task}/cancel``, refused (409
    ``not_claim_holder``) unless the build holds the task's claim.
    """
    _member_action(
        "cancel", task_id, build, yes, stardag_profile, stardag_env, json_output
    )


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
    if result.build_failed:
        console.print("[bold red]A root was excluded: the build failed.[/bold red]")
