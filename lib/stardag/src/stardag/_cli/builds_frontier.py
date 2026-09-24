"""``stardag builds frontier`` and ``builds ticks``: what a reactive
scheduler tick sees, and what each tick decided. Split from
:mod:`stardag._cli.builds` by the module-size rule; registered on its
``app``.
"""

from typing import Optional

import typer
from rich.table import Table

from stardag._cli._output import JSON_OPTION, emit_json, parse_uuid, stamp, task_label
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
)
from stardag.exceptions import StardagError
from stardag.registry import FrontierMember, PlanDetail

_BUILD_ID = typer.Argument(..., help="Build ID")


def _parse_build_id(value: str):
    return parse_uuid(value, "build ID")


# -----------------------------------------------------------------------------
# frontier / ticks
# -----------------------------------------------------------------------------


def _render_members(
    title: str, members: list[FrontierMember], *, counts: bool = False
) -> None:
    """One frontier list; ``counts`` adds the ledger counts the server
    serves on runnable and running items (D9)."""
    if not members:
        return
    table = Table(title=title)
    for col in ("Task ID", "Task", "Status", "Root"):
        table.add_column(col)
    if counts:
        table.add_column("Attempts")
        table.add_column("Interruptions")
    for member in members:
        row = [
            member.task_id,
            task_label(member.body),
            member.status,
            "yes" if member.is_root else "",
        ]
        if counts:
            row += [str(member.attempts), str(member.interruptions)]
        table.add_row(*row)
    console.print(table)


def builds_frontier(
    build_id: str = _BUILD_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show a build's active plan as a scheduler tick sees it: the three
    lists, whether the build is flagged for a tick, the members by status
    and how many roots are complete.

    Reads ``GET /builds/{id}/frontier`` (which runs the closure step
    first), ``GET /builds/{id}/notify`` (the wake-up flag, read without
    clearing it), and for the active plan ``GET /plans/{id}`` (counts) and
    ``GET /plans/{id}/roots``. Writes nothing.
    """
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    plan: PlanDetail | None = None
    roots: list[FrontierMember] = []
    try:
        frontier = registry.build_get_frontier(parsed)
        needs_tick = registry.build_get_notify(parsed).needs_tick
        if frontier.plan_id is not None:
            plan = registry.plan_get(frontier.plan_id)
            roots = registry.plan_roots_info(frontier.plan_id).roots
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    roots_completed = sum(1 for r in roots if r.status == "completed")
    if json_output:
        emit_json(
            {
                **frontier.model_dump(mode="json"),
                "needs_tick": needs_tick,
                "member_counts": plan.member_counts if plan else None,
                "excluded_count": plan.excluded_count if plan else None,
                "roots_completed": roots_completed if plan else None,
                "roots_total": len(roots) if plan else None,
            }
        )
        return
    summary = Table(title=f"Frontier of build {frontier.build_id}", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Build status", frontier.build_status or "-")
    summary.add_row(
        "Reactive app", frontier.reactive_app_name or "- (not reactively scheduled)"
    )
    summary.add_row("Needs tick", "yes" if needs_tick else "no")
    summary.add_row("Plan", str(frontier.plan_id) if frontier.plan_id else "- (none)")
    summary.add_row(
        "Deployment", str(frontier.deployment_id) if frontier.deployment_id else "-"
    )
    summary.add_row("Settings", frontier.settings_hash or "-")
    summary.add_row("Sealed", "yes" if frontier.sealed else "no")
    summary.add_row("Plan complete", "yes" if frontier.plan_complete else "no")
    if plan is not None:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(plan.member_counts.items()))
        if plan.excluded_count:
            counts = f"{counts or '-'} (+{plan.excluded_count} excluded)"
        summary.add_row("Members", counts or "-")
        summary.add_row("Roots", f"{roots_completed}/{len(roots)} completed")
    summary.add_row("Discovery jobs", str(len(frontier.discovery_jobs)))
    summary.add_row("Runnable", str(len(frontier.runnable)))
    summary.add_row("Running", str(len(frontier.running)))
    console.print(summary)
    _render_members("Discovery jobs", frontier.discovery_jobs)
    _render_members("Runnable", frontier.runnable, counts=True)
    _render_members("Running", frontier.running, counts=True)
    if frontier.closure is not None and frontier.closure.conflicts:
        conflicts = ", ".join(c.task_id for c in frontier.closure.conflicts)
        console.print(f"[bold red]Closure conflicts:[/bold red] {conflicts}")


def builds_ticks(
    build_id: str = _BUILD_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, max=200, help="Summaries to show (newest first)."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Show the reactive scheduler's own account of its recent ticks.

    Reads ``GET /builds/{id}/tick-summaries``.
    """
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        summaries = registry.build_list_tick_summaries(parsed, limit=limit)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json({"summaries": [s.model_dump(mode="json") for s in summaries]})
        return
    if not summaries:
        console.print(f"No tick summaries recorded for build {build_id}.")
        console.print("\n[dim]Only reactively-scheduled builds report them.[/dim]")
        return
    table = Table(title=f"Tick summaries for build {build_id} (newest first)")
    table.add_column("When")
    table.add_column("Outcome")
    table.add_column("Detail")
    for record in summaries:
        detail = ", ".join(
            f"{k}={v}"
            for k, v in sorted(record.summary.items())
            if k != "outcome" and v not in (0, None)
        )
        table.add_row(stamp(record.created_at), record.outcome, detail or "-")
    console.print(table)
