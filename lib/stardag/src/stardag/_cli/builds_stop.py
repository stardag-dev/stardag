"""``stardag builds stop``: stop a build's unended executions, report each
one stopped, then cancel the build. Selection rules: :mod:`._stop`."""

from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from stardag._cli import _stop
from stardag._cli._duration import parse_duration
from stardag._cli._output import (
    JSON_OPTION,
    YES_OPTION,
    age,
    emit_json,
    parse_uuid,
    short,
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
from stardag.registry import ExecutionInfo


def builds_stop(
    build_id: str = typer.Argument(..., help="Build ID"),
    not_in_current_plan: bool = typer.Option(
        False,
        "--not-in-current-plan",
        help="Only orphans: executions started under a plan that is no longer "
        "the build's active one (after a rollover or a re-trigger under new "
        "settings). Implies --no-cancel: the build itself keeps running.",
    ),
    executor: Optional[str] = typer.Option(
        None, "--executor", help="Only executions on this executor, e.g. 'modal'."
    ),
    worker: Optional[str] = typer.Option(
        None, "--worker", help="Only executions on this worker (the app's name for it)."
    ),
    older_than: Optional[str] = typer.Option(
        None,
        "--older-than",
        help="Only executions started at least this long ago (e.g. 30m, 6h, 2d).",
    ),
    task_id: Optional[list[str]] = typer.Option(
        None, "--task-id", help="Only this task (repeatable)."
    ),
    no_cancel: bool = typer.Option(
        False, "--no-cancel", help="Stop and report the executions; leave the build."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the list and exit; stop and write nothing."
    ),
    yes: bool = YES_OPTION,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Stop a build's unended executions, then cancel the build.

    Reads ``GET /builds/{id}/executions`` — the execution ledger's rows
    with no end reported, which is exact whatever has happened to their
    claims — and, after confirmation:

    \b
    1. cancels each selected Modal function call (``executor_ref``);
    2. reports each one it cancelled with
       ``POST /executions/{id}/stopped`` (outcome ``stopped``), which ends
       it on the ledger and releases its claim if it still holds one;
    3. cancels the build (``POST /builds/{id}/cancel``) unless
       ``--no-cancel`` or ``--not-in-current-plan``.

    Executions on another executor, in a driver's own process, or whose
    spawn has not reported a call id yet are listed with the reason and
    left alone; a call that could not be cancelled is not reported
    stopped. Hard kills are the Modal dashboard's job.
    """
    parsed = parse_uuid(build_id, "build ID")
    older_than_seconds: int | None = None
    if older_than is not None:
        try:
            older_than_seconds = parse_duration(older_than)
        except ValueError as e:
            error_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(1)
    filters = _stop.Filters(
        executor=executor,
        worker=worker,
        older_than_seconds=older_than_seconds,
        task_ids=tuple(task_id or ()),
    )
    cancel_build = not (no_cancel or not_in_current_plan)

    if json_output and not dry_run and not yes:
        error_console.print(
            "[bold red]Error:[/bold red] refusing to prompt in --json mode; "
            "pass --yes to confirm."
        )
        raise typer.Exit(1)

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        try:
            executions = registry.build_list_executions(
                parsed, not_in_current_plan=not_in_current_plan
            )
        except StardagError as e:
            _fail(e)
        selected, excluded = _stop.split(executions, filters)
        stoppable = [e for e in selected if _stop.is_stoppable(e)]
        unstoppable = [e for e in selected if not _stop.is_stoppable(e)]
        payload: dict[str, Any] = {
            "build_id": str(parsed),
            "not_in_current_plan": not_in_current_plan,
            "selected": [_stop.execution_json(e) for e in selected],
            "excluded_by_filter": [_stop.execution_json(e) for e in excluded],
            "dry_run": dry_run,
        }
        if dry_run:
            if json_output:
                emit_json(payload)
            else:
                _render(selected, excluded, build_id, not_in_current_plan)
                console.print(
                    "\n[bold]Dry run — nothing was stopped or written.[/bold]"
                )
            return
        if not json_output:
            _render(selected, excluded, build_id, not_in_current_plan)
        if not selected and not cancel_build:
            return
        if not yes:
            typer.confirm(
                _confirmation(build_id, stoppable, unstoppable, excluded, cancel_build),
                abort=True,
            )
        # In --json mode progress goes to stderr: stdout stays one document,
        # written at the end, when everything it states has happened.
        report = error_console if json_output else console
        results = _stop_and_report(registry, stoppable, report)
        if cancel_build:
            try:
                registry.build_cancel(parsed)
            except StardagError as e:
                _fail(e)
        payload["stop_results"] = results
        payload["stopped_count"] = sum(1 for r in results if r["reported"])
        payload["build_cancelled"] = cancel_build
    finally:
        registry.close()

    if json_output:
        emit_json(payload)
    stopped = payload["stopped_count"]
    if cancel_build:
        report.print(
            f"[green]Cancelled build[/green] {build_id} — stopped {stopped} "
            "execution(s)."
        )
    else:
        report.print(
            f"[green]Stopped {stopped} execution(s)[/green] of build {build_id}; "
            "the build was not cancelled."
        )
    if unstoppable:
        report.print(
            f"[yellow]{len(unstoppable)} selected execution(s) could not be "
            "stopped[/yellow] and keep running:"
        )
        for execution in unstoppable:
            report.print(
                f"  [dim]{execution.task_id}  "
                f"{escape(_stop.not_stoppable_reason(execution) or '')}[/dim]"
            )
    if excluded:
        report.print(
            f"[dim]{len(excluded)} execution(s) were excluded by a filter and "
            "keep running.[/dim]"
        )


def _stop_and_report(
    registry, stoppable: Sequence[ExecutionInfo], report: Console
) -> list[dict[str, Any]]:
    """Cancel the calls, then report each cancelled one to the ledger. A
    failure on one never aborts the rest."""
    if not stoppable:
        return []
    try:
        outcomes = _stop.cancel_modal_calls(stoppable)
    except _stop.ModalUnavailable as e:
        error_console.print(f"[bold red]Error:[/bold red] {e}")
        error_console.print(
            "[dim]Nothing was stopped or reported, and the build was not "
            "cancelled; the command can be re-run.[/dim]"
        )
        raise typer.Exit(1)
    results: list[dict[str, Any]] = []
    for outcome in outcomes:
        execution = outcome.execution
        result: dict[str, Any] = {
            "execution_id": str(execution.id),
            "task_id": execution.task_id,
            "executor_ref": execution.executor_ref,
            "stopped": outcome.ok,
            "reported": False,
            "error": outcome.error,
        }
        ref = execution.executor_ref or "(no call id)"
        if outcome.ok:
            try:
                registry.execution_report_stopped(execution.id)
            except StardagError as e:
                result["error"] = f"stopped, but the report failed: {e}"
                report.print(
                    f"  [yellow]stopped, not reported[/yellow] {ref}  {escape(str(e))}"
                )
            else:
                result["reported"] = True
                report.print(f"  [green]stopped[/green] {ref}  {execution.task_id}")
        else:
            report.print(
                f"  [red]failed[/red]  {ref}  {execution.task_id}  "
                f"{escape(outcome.error or '')}"
            )
        results.append(result)
    if any(not r["reported"] for r in results):
        error_console.print(
            "[bold yellow]Warning:[/bold yellow] some executions were not "
            "stopped or not reported (above). A call that could not be "
            "cancelled may already have ended; if not, stop it from the Modal "
            "dashboard. Re-running reports any that are still unended."
        )
    return results


def _render(
    selected: Sequence[ExecutionInfo],
    excluded: Sequence[ExecutionInfo],
    build_id: str,
    not_in_current_plan: bool,
) -> None:
    what = "orphaned executions" if not_in_current_plan else "unended executions"
    if not selected:
        suffix = f" ({len(excluded)} excluded by the filters)" if excluded else ""
        console.print(f"Build {build_id} has no {what} to stop{suffix}.")
        return
    now = datetime.now(timezone.utc)
    table = Table(title=f"{what.capitalize()} of build {build_id}")
    for col in ("Execution", "Task ID", "Plan", "Executor", "Ref", "Worker", "Age"):
        table.add_column(col)
    for e in selected:
        executor = _stop.executor_of(e) or "-"
        reason = _stop.not_stoppable_reason(e)
        table.add_row(
            short(e.id),
            e.task_id or "-",
            short(e.plan_id, 8) + ("" if e.in_current_plan else " (orphan)"),
            executor
            if reason is None
            else f"{executor} [yellow](not stoppable)[/yellow]",
            e.executor_ref or "-",
            _stop.worker_of(e) or "-",
            age(e.started_at, now),
        )
    console.print(table)
    workspaces = _stop.modal_workspaces(e for e in selected if _stop.is_stoppable(e))
    if workspaces:
        console.print(
            "[dim]Modal workspace(s): "
            + ", ".join(sorted(workspaces))
            + " — the active Modal profile must be authenticated to them.[/dim]"
        )


def _confirmation(
    build_id: str,
    stoppable: Sequence[ExecutionInfo],
    unstoppable: Sequence[ExecutionInfo],
    excluded: Sequence[ExecutionInfo],
    cancel_build: bool,
) -> str:
    action = f"Stop {len(stoppable)} execution(s)"
    action += f" and cancel build {build_id}?" if cancel_build else "?"
    left = len(unstoppable) + len(excluded)
    if not left:
        return action
    return f"{left} execution(s) will keep running. {action}"
