"""``stardag deployments``: the deployments recorded in the registry (v2).

One row per ``stardag modal deploy`` (``kind=modal``), plus one per local
code id a local build planned under (``kind=local``). An app's **current**
deployment is its activated Modal row with the highest generation; local
rows are never current (design.md, "The deterministic scope").
"""

from typing import Optional, cast

import typer
from rich.table import Table

from stardag._cli._output import JSON_OPTION, emit_json, short, stamp
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError
from stardag.registry import DeploymentInfo
from stardag.registry._models import DeploymentKind

app = typer.Typer(help="Inspect recorded deployments.", no_args_is_help=True)


def check_kind(kind: Optional[str]) -> None:
    if kind not in (None, "modal", "local"):
        error_console.print(
            f"[bold red]Error:[/bold red] --kind is 'modal' or 'local', got {kind!r}."
        )
        raise typer.Exit(1)


def render_deployments(rows: list[DeploymentInfo], *, json_output: bool) -> None:
    """The listing, shared with ``stardag modal deployments``."""
    if json_output:
        emit_json({"deployments": [d.model_dump(mode="json") for d in rows]})
        return
    if not rows:
        console.print("No deployments recorded.")
        return
    table = Table(title="Deployments (newest first)")
    for col in (
        "Kind",
        "App",
        "Deployment",
        "Gen",
        "Code id",
        "Deployed",
        "Activated",
        "",
    ):
        table.add_column(col)
    for d in rows:
        if d.is_current:
            mark = "[green]current[/green]"
        elif d.kind == "modal" and d.activated_at is None:
            mark = "[yellow]not activated[/yellow]"
        else:
            mark = ""
        table.add_row(
            d.kind,
            d.app_name,
            str(d.id),
            str(d.generation),
            short(d.code_id),
            stamp(d.deployed_at),
            stamp(d.activated_at),
            mark,
        )
    console.print(table)


@app.command("list")
def deployments_list(
    app_name: Optional[str] = typer.Option(None, "--app", help="Only this app."),
    kind: Optional[str] = typer.Option(
        None, "--kind", help="Only 'modal' or only 'local' deployments."
    ),
    current: bool = typer.Option(
        False, "--current", help="Only each app's current deployment."
    ),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=500),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List deployments, newest first, marking each app's current one.

    Reads ``GET /deployments``. Writes nothing. A Modal row that is not
    activated is a deploy whose record was created but whose activation
    never landed: until it does, the new code's ticks exit ``superseded``
    (re-run ``stardag modal deploy``).
    """
    check_kind(kind)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        rows = registry.deployment_list(
            kind=cast(Optional[DeploymentKind], kind),
            app_name=app_name,
            current=current,
            limit=limit,
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    render_deployments(rows, json_output=json_output)
