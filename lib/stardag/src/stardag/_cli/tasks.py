"""Task inspection and recovery commands for the Stardag CLI.

    stardag tasks list [--status running] [--older-than 1h]
    stardag tasks cancel <build-id> <task-id>
    stardag tasks retry <build-id> <task-id>

``tasks list --status running`` is the claim-holder question. A task row is
unique per ``(environment_id, task_id)`` and its status is denormalised
there, so a task left RUNNING by a build whose orchestrator died denies
the execution claim to *every* future build that needs it, indefinitely —
and keeps occupying whatever concurrency-limit slots it acquired.
``latest_status_build_id`` names the build holding it and
``latest_status_at`` says since when, which together are the whole
diagnosis. ``--status suspended`` matters for the same reason: an
abandoned suspension gates everything downstream of it.

Cancel and retry take ``<build-id> <task-id>`` because a task event is
recorded *against a build*: the registry's task lifecycle endpoints are
build sub-resources, and the build whose event produced a task's current
status is the one entitled to change it. Use the build id from
``latest_status_build_id`` (``tasks list`` prints it) — that is the claim
holder, and cancelling from anywhere else records an event for a build
that never ran the task.

``--json`` on the read-only command follows the convention documented in
``stardag._cli.builds``: the SDK's model of the API payload, alone on
stdout.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import typer
from rich.table import Table

# Shared by every registry-backed CLI group; imported into this module's
# namespace so ``stardag._cli.tasks._resolve_registry`` is the patch point.
from stardag._cli._duration import format_duration, parse_duration
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError

app = typer.Typer(
    help="Inspect, cancel and retry tasks in an environment",
    no_args_is_help=True,
)

_JSON_OPTION = typer.Option(
    False,
    "--json",
    help="Emit the API payload as JSON on stdout (nothing else goes to stdout).",
)


def _emit_json(payload: Any) -> None:
    """Write one JSON document to stdout, and nothing else."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age(value: datetime | None) -> str:
    if value is None:
        return "-"
    delta = datetime.now(timezone.utc) - _as_utc(value)
    return format_duration(delta.total_seconds())


def _stamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return _as_utc(value).strftime("%Y-%m-%d %H:%M:%SZ")


def _parse_uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        error_console.print(
            f"[bold red]Error:[/bold red] {value!r} is not a valid {label} (UUID)."
        )
        raise typer.Exit(1)


@app.command("list")
def tasks_list(
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    status: Optional[list[str]] = typer.Option(
        None,
        "--status",
        help=(
            "Global task status; repeatable "
            "(--status running --status suspended matches either)."
        ),
    ),
    older_than: Optional[str] = typer.Option(
        None,
        "--older-than",
        help="Only tasks in their current status at least this long (e.g. 1h, 3d).",
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Filter by task name."),
    namespace: Optional[str] = typer.Option(
        None, "--namespace", help="Filter by task namespace."
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number (1-based)."),
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, max=100, help="Tasks per page (max 100)."
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """List tasks by their environment-global status — i.e. claim holders.

    With a status or staleness filter the server returns oldest-claim
    first: the task that has been RUNNING longest is both the most likely
    to be abandoned and the most expensive to leave holding a claim.

    ``--older-than`` is converted to an absolute cutoff before it is sent,
    so paging a large result cannot have the cutoff drift underneath it.
    Tasks with no recorded status timestamp never match it — an age that
    cannot be established is not evidence of staleness.
    """
    status_older_than: datetime | None = None
    if older_than is not None:
        try:
            seconds = parse_duration(older_than)
        except ValueError as e:
            error_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(1)
        status_older_than = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.task_list(
            page=page,
            page_size=limit,
            status=status or None,
            status_older_than=status_older_than,
            task_name=name,
            task_namespace=namespace,
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    if json_output:
        _emit_json(
            {
                "tasks": [t.model_dump(mode="json") for t in result.tasks],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
            }
        )
        return

    if not result.tasks:
        console.print("No tasks match this filter.")
        console.print(
            "\n[dim]Claim holders are: stardag tasks list --status running[/dim]"
        )
        return

    table = Table(title=f"Tasks (page {result.page}, {result.total} total)")
    table.add_column("Task ID")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Since")
    table.add_column("For", justify="right")
    table.add_column("Held by build")
    table.add_column("Executor")
    for task in result.tasks:
        qualified = (
            f"{task.task_namespace}.{task.task_name}"
            if task.task_namespace
            else task.task_name
        )
        table.add_row(
            task.task_id,
            qualified,
            task.latest_status or "-",
            _stamp(task.latest_status_at),
            _age(task.latest_status_at),
            str(task.latest_status_build_id or "-"),
            task.latest_executor or "-",
        )
    console.print(table)
    if result.total > len(result.tasks):
        console.print(
            f"[dim]Showing {len(result.tasks)} of {result.total} "
            f"(use --page / --limit for more).[/dim]"
        )


@app.command("cancel")
def tasks_cancel(
    build_id: str = typer.Argument(..., help="Build the event is recorded against"),
    task_id: str = typer.Argument(..., help="Task ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Cancel a task, releasing its execution claim and limit slots.

    This is the recovery for a task stranded RUNNING or SUSPENDED by a
    build that is gone. Pass the build from ``latest_status_build_id`` —
    the claim holder.

    The server cannot stop anything: a worker still executing keeps going
    until it notices, and a completion that lands afterwards wins
    (COMPLETED is sticky). Cancel claims whose process you believe is
    dead.
    """
    parsed_build = _parse_uuid(build_id, "build ID")
    if not yes:
        typer.confirm(
            f"Cancel task {task_id} in build {build_id}? "
            "This releases its execution claim and any limit slots.",
            abort=True,
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        registry.task_cancel_by_id(parsed_build, task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(f"[green]Cancelled task[/green] {task_id} in build {build_id}")


@app.command("retry")
def tasks_retry(
    build_id: str = typer.Argument(..., help="Build the event is recorded against"),
    task_id: str = typer.Argument(..., help="Task ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Reset a terminal-but-retryable task to PENDING so it can run again.

    Flips failed / cancelled / skipped / suspended back to PENDING; a
    COMPLETED or RUNNING task is untouched (a RUNNING task holds a live
    claim, and releasing that is cancellation, not retry — see
    ``stardag tasks cancel``).
    """
    parsed_build = _parse_uuid(build_id, "build ID")
    if not yes:
        typer.confirm(
            f"Reset task {task_id} in build {build_id} to PENDING?",
            abort=True,
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        registry.task_retry_by_id(parsed_build, task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(f"[green]Reset task[/green] {task_id} to pending in build {build_id}")
