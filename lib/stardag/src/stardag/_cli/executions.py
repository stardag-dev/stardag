"""``stardag executions``: the execution ledger (registry v2).

One ledger row per claim granted; its two ends are written by two hands —
the claim's release by the registry, the execution's own end by its report
or an operator stop (design.md, "``execution`` — the ledger").

    stardag executions list --build <id> [--not-in-current-plan] [--include-ended]
    stardag executions list --task <id> [--include-ended]
"""

from datetime import datetime, timezone
from typing import Optional

import typer
from rich.table import Table

from stardag._cli import _stop
from stardag._cli._output import JSON_OPTION, age, emit_json, parse_uuid, short, stamp
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError
from stardag.registry import ExecutionInfo

app = typer.Typer(help="Inspect the execution ledger.", no_args_is_help=True)


@app.command("list")
def executions_list(
    build: Optional[str] = typer.Option(None, "--build", "-b", help="Build ID"),
    task: Optional[str] = typer.Option(
        None, "--task", "-t", help="Task ID: its executions across builds."
    ),
    include_ended: bool = typer.Option(
        False,
        "--include-ended",
        help="The whole ledger, ended executions included (default: only "
        "those with no end reported).",
    ),
    not_in_current_plan: bool = typer.Option(
        False,
        "--not-in-current-plan",
        help="Only orphans: executions of a plan that is no longer the "
        "build's active one (with --build).",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List executions from the ledger: a build's (``--build``), or a task's
    across builds, newest first (``--task``). By default only those with no
    end reported — what ``builds stop`` would act on; ``--include-ended``
    lists every execution granted.

    Reads ``GET /builds/{id}/executions`` or ``GET /tasks/{id}/executions``.
    Writes nothing.
    """
    if (build is None) == (task is None):
        error_console.print(
            "[bold red]Error:[/bold red] pass exactly one of --build and --task."
        )
        raise typer.Exit(1)
    if not_in_current_plan and task is not None:
        error_console.print(
            "[bold red]Error:[/bold red] --not-in-current-plan needs --build "
            "(orphans are relative to a build's active plan)."
        )
        raise typer.Exit(1)
    build_id = parse_uuid(build, "build ID") if build is not None else None
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        if build_id is not None:
            rows = registry.build_list_executions(
                build_id,
                not_in_current_plan=not_in_current_plan,
                include_ended=include_ended,
            )
        else:
            assert task is not None
            rows = registry.task_list_executions(task, include_ended=include_ended)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    subject = f"build {build_id}" if build_id is not None else f"task {task}"
    if json_output:
        emit_json(
            {
                **(
                    {"build_id": str(build_id)}
                    if build_id is not None
                    else {"task_id": task}
                ),
                "executions": [_stop.execution_json(e) for e in rows],
            }
        )
        return
    if not rows:
        what = (
            "orphaned"
            if not_in_current_plan
            else ("recorded" if include_ended else "unended")
        )
        console.print(f"{subject.capitalize()} has no {what} executions.")
        return
    render_executions(
        rows,
        title=f"{'Executions' if include_ended else 'Unended executions'} of {subject}",
        by_task=task is not None,
    )


def render_executions(rows: list[ExecutionInfo], *, title: str, by_task: bool) -> None:
    """The ledger as a table; shared with ``tasks show``. ``by_task`` shows
    the build column instead of the task column."""
    now = datetime.now(timezone.utc)
    table = Table(title=title)
    first = "Build" if by_task else "Task ID"
    for col in (
        "Execution",
        first,
        "Plan",
        "Executor",
        "Ref",
        "Claim",
        "Started",
        "Ended",
        "Stoppable",
    ):
        table.add_column(col)
    for e in rows:
        claim = "held" if e.claim_released_at is None else e.claim_outcome or "released"
        ended = "-" if e.ended_at is None else f"{e.outcome or '?'} {stamp(e.ended_at)}"
        table.add_row(
            str(e.id),
            (short(e.build_id, 8) if by_task else e.task_id) or "-",
            short(e.plan_id, 8) + ("" if e.in_current_plan else " (orphan)"),
            _stop.executor_of(e) or "-",
            e.executor_ref or "-",
            claim,
            age(e.started_at, now) + " ago",
            ended,
            "yes" if _stop.is_stoppable(e) else "no",
        )
    console.print(table)
