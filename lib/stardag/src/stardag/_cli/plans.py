"""``stardag plans``: one build's request under one scope (registry v2).

A plan is identified by its build and its scope ``(deployment, settings)``;
a build's active plan is the one its frontier is computed over, and a
replacement (a rollover, a re-trigger under new settings) supersedes it
when it seals (design.md, "``plan``").

    stardag plans show <plan-id>          # lifecycle, scope, counts, roots
    stardag plans list --build <build-id> # a build's plans, newest first
"""

from typing import Any, Optional

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
from stardag.exceptions import NotFoundError, StardagError
from stardag.registry import PlanDetail

app = typer.Typer(help="Inspect plans.", no_args_is_help=True)


def _lifecycle(plan: PlanDetail) -> str:
    if plan.superseded_at is not None:
        return f"superseded {stamp(plan.superseded_at)}"
    if plan.is_active:
        return "active"
    return "not active — a replacement not yet sealed"


def _counts(plan: PlanDetail) -> str:
    counts = ", ".join(f"{k}={v}" for k, v in sorted(plan.member_counts.items()))
    if plan.excluded_count:
        counts = f"{counts or '-'} (+{plan.excluded_count} excluded)"
    return counts or "-"


@app.command("show")
def plans_show(
    plan_id: str = typer.Argument(..., help="Plan ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show a plan — active or superseded: its build, lifecycle (created,
    activated, sealed, superseded), scope (deployment, settings), member
    counts by status (excluded apart), and its roots; for the active plan,
    what is outstanding.

    Reads ``GET /plans/{id}``, ``GET /plans/{id}/roots``,
    ``GET /settings/{hash}`` and, for the active plan only,
    ``GET /builds/{build}/frontier``. Writes nothing.
    """
    parsed = parse_uuid(plan_id, "plan ID")
    registry = _resolve_registry(stardag_profile, stardag_env)
    settings: dict[str, str] | None = None
    outstanding: dict[str, int] | None = None
    plan_complete: bool | None = None
    try:
        plan = registry.plan_get(parsed)
        roots = registry.plan_roots_info(parsed).roots
        try:
            settings = registry.settings_get(plan.settings_hash).body
        except NotFoundError:
            settings = None
        if plan.is_active:
            frontier = registry.build_get_frontier(plan.build_id)
            if frontier.plan_id == plan.id:
                plan_complete = frontier.plan_complete
                outstanding = {
                    "discovery_jobs": len(frontier.discovery_jobs),
                    "runnable": len(frontier.runnable),
                    "running": len(frontier.running),
                }
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    payload: dict[str, Any] = {
        **plan.model_dump(mode="json"),
        "settings": settings,
        "roots": [r.model_dump(mode="json") for r in roots],
        "plan_complete": plan_complete,
        "outstanding": outstanding,
    }
    if json_output:
        emit_json(payload)
        return
    table = Table(title=f"Plan {plan.id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Build", str(plan.build_id))
    table.add_row("Generation", str(plan.generation))
    table.add_row("Lifecycle", _lifecycle(plan))
    table.add_row("Created", stamp(plan.created_at))
    table.add_row("Activated", stamp(plan.activated_at))
    table.add_row("Sealed", stamp(plan.sealed_at) if plan.sealed_at else "no")
    deployment = str(plan.deployment_id)
    if plan.deployment is not None:
        d = plan.deployment
        deployment += f" ({d.kind} {d.app_name} gen {d.generation}"
        deployment += ", current)" if d.is_current else ")"
    table.add_row("Deployment", deployment)
    table.add_row("Settings hash", plan.settings_hash)
    if settings:
        table.add_row(
            "Settings", ", ".join(f"{k}={v}" for k, v in sorted(settings.items()))
        )
    table.add_row("Members", f"{plan.member_count}: {_counts(plan)}")
    if outstanding is not None:
        table.add_row("Plan complete", "yes" if plan_complete else "no")
        table.add_row(
            "Outstanding",
            f"{outstanding['discovery_jobs']} to discover, "
            f"{outstanding['runnable']} runnable, {outstanding['running']} running",
        )
    console.print(table)
    rows = Table(title=f"Roots ({plan.root_count})")
    for col in ("Task ID", "Task", "Status"):
        rows.add_column(col)
    for root in roots:
        rows.add_row(root.task_id, task_label(root.body), root.status)
    console.print(rows)


@app.command("list")
def plans_list(
    build: str = typer.Option(..., "--build", "-b", help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List a build's plans, newest generation first, with lifecycle and
    member counts.

    Reads ``GET /builds/{id}/plans``. Writes nothing.
    """
    build_id = parse_uuid(build, "build ID")
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        plans = registry.build_list_plans(build_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(
            {
                "build_id": str(build_id),
                "plans": [p.model_dump(mode="json") for p in plans],
            }
        )
        return
    if not plans:
        console.print(f"Build {build_id} has no plan yet.")
        return
    table = Table(title=f"Plans of build {build_id} (newest first)")
    for col in ("Plan", "Gen", "Lifecycle", "Deployment", "Settings", "Members"):
        table.add_column(col)
    for p in plans:
        table.add_row(
            str(p.id),
            str(p.generation),
            _lifecycle(p),
            str(p.deployment_id),
            p.settings_hash[:12],
            f"{p.member_count}: {_counts(p)}",
        )
    console.print(table)
