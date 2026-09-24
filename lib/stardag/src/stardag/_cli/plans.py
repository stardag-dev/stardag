"""``stardag plans``: one build's request under one scope (registry v2).

A plan is identified by its build and its scope ``(deployment, settings)``;
a build's active plan is the one its frontier is computed over, and a
replacement (a rollover, a re-trigger under new settings) supersedes it
when it seals (design.md, "``plan``").
"""

from typing import Any, Optional

import typer
from rich.table import Table

from stardag._cli._output import JSON_OPTION, emit_json, parse_uuid, task_label
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
)
from stardag.exceptions import StardagError

app = typer.Typer(help="Inspect plans.", no_args_is_help=True)


@app.command("show")
def plans_show(
    plan_id: str = typer.Argument(..., help="Plan ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show a plan: its build, scope, roots, whether it is the build's
    active plan and, if so, whether it is sealed and what is outstanding.

    Reads ``GET /plans/{id}/roots``, ``GET /settings/{hash}`` and
    ``GET /builds/{build}/frontier``. Writes nothing. The registry serves
    no plan read with its timestamps or member counts, so for a plan that
    is not active, sealed/activated/superseded are reported as unknown.
    """
    parsed = parse_uuid(plan_id, "plan ID")
    registry = _resolve_registry(stardag_profile, stardag_env)
    settings: dict[str, str] | None = None
    try:
        info = registry.plan_roots_info(parsed)
        try:
            settings = registry.settings_get(info.settings_hash).body
        except StardagError:
            settings = None
        frontier = registry.build_get_frontier(info.build_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    active = frontier.plan_id == info.plan_id
    payload: dict[str, Any] = {
        **info.model_dump(mode="json"),
        "settings": settings,
        "active": active,
        "sealed": frontier.sealed if active else None,
        "plan_complete": frontier.plan_complete if active else None,
        "outstanding": {
            "discovery_jobs": len(frontier.discovery_jobs),
            "runnable": len(frontier.runnable),
            "running": len(frontier.running),
        }
        if active
        else None,
    }
    if json_output:
        emit_json(payload)
        return
    table = Table(title=f"Plan {info.plan_id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Build", str(info.build_id))
    table.add_row("Deployment", str(info.deployment_id))
    table.add_row("Settings hash", info.settings_hash)
    if settings:
        table.add_row(
            "Settings", ", ".join(f"{k}={v}" for k, v in sorted(settings.items()))
        )
    if active:
        table.add_row("Active", "yes")
        table.add_row("Sealed", "yes" if frontier.sealed else "no")
        table.add_row("Plan complete", "yes" if frontier.plan_complete else "no")
        table.add_row(
            "Outstanding",
            f"{len(frontier.discovery_jobs)} to discover, "
            f"{len(frontier.runnable)} runnable, {len(frontier.running)} running",
        )
    else:
        table.add_row(
            "Active",
            "no — superseded, or a replacement not yet sealed "
            f"(the build's active plan is {frontier.plan_id or 'none'})",
        )
    console.print(table)
    roots = Table(title="Roots")
    for col in ("Task ID", "Task", "Status"):
        roots.add_column(col)
    for root in info.roots:
        roots.add_row(root.task_id, task_label(root.body), root.status)
    console.print(roots)
