"""``stardag executions``: the execution ledger (registry v2).

One ledger row per claim granted; its two ends are written by two hands —
the claim's release by the registry, the execution's own end by its report
or an operator stop (design.md, "``execution`` — the ledger").
"""

from datetime import datetime, timezone
from typing import Optional

import typer
from rich.table import Table

from stardag._cli import _stop
from stardag._cli._output import JSON_OPTION, age, emit_json, parse_uuid, short
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
)
from stardag.exceptions import StardagError

app = typer.Typer(help="Inspect the execution ledger.", no_args_is_help=True)


@app.command("list")
def executions_list(
    build: str = typer.Option(..., "--build", "-b", help="Build ID"),
    not_in_current_plan: bool = typer.Option(
        False,
        "--not-in-current-plan",
        help="Only orphans: executions of a plan that is no longer the "
        "build's active one.",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List a build's executions with no end reported — what
    ``builds stop`` would act on.

    Reads ``GET /builds/{id}/executions``. Writes nothing.
    """
    build_id = parse_uuid(build, "build ID")
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        rows = registry.build_list_executions(
            build_id, not_in_current_plan=not_in_current_plan
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(
            {
                "build_id": str(build_id),
                "executions": [_stop.execution_json(e) for e in rows],
            }
        )
        return
    if not rows:
        what = "orphaned" if not_in_current_plan else "unended"
        console.print(f"Build {build_id} has no {what} executions.")
        return
    now = datetime.now(timezone.utc)
    table = Table(title=f"Unended executions of build {build_id}")
    for col in (
        "Execution",
        "Task ID",
        "Plan",
        "Executor",
        "Ref",
        "Claim",
        "Started",
        "Stoppable",
    ):
        table.add_column(col)
    for e in rows:
        claim = "held" if e.claim_released_at is None else e.claim_outcome or "released"
        table.add_row(
            str(e.id),
            e.task_id or "-",
            short(e.plan_id, 8) + ("" if e.in_current_plan else " (orphan)"),
            _stop.executor_of(e) or "-",
            e.executor_ref or "-",
            claim,
            age(e.started_at, now) + " ago",
            "yes" if _stop.is_stoppable(e) else "no",
        )
    console.print(table)
