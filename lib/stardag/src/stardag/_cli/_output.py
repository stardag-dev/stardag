"""Output and argument helpers shared by the registry-backed CLI groups
(``build``, ``builds``, ``executions``, ``plans``, ``deployments``,
``tasks``).

``--json`` contract: stdout carries exactly one JSON document — the SDK's
model of the API payload — and every hint, warning and prompt goes to
stderr, so piping to ``jq`` is safe.
"""

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import typer
from rich.markup import escape

from stardag._cli._registry_ctx import error_console

JSON_OPTION = typer.Option(
    False,
    "--json",
    help="Emit the API payload as JSON on stdout (nothing else goes to stdout).",
)

YES_OPTION = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")


def emit_json(payload: Any) -> None:
    """Write one JSON document to stdout, and nothing else."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def as_utc(value: datetime) -> datetime:
    """Interpret a naive timestamp as UTC (the API's own convention)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def stamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return as_utc(value).strftime("%Y-%m-%d %H:%M:%SZ")


def age(value: datetime | None, now: datetime | None = None) -> str:
    """``3h12m``-style age of ``value`` (``-`` when unknown)."""
    if value is None:
        return "-"
    seconds = int(((now or datetime.now(timezone.utc)) - as_utc(value)).total_seconds())
    if seconds < 0:
        return "0s"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def parse_uuid(value: str, what: str = "build ID") -> UUID:
    """Parse a UUID argument, failing with a CLI error rather than a stack
    trace."""
    try:
        return UUID(value)
    except ValueError:
        error_console.print(
            f"[bold red]Error:[/bold red] {escape(repr(value))} is not a valid "
            f"{what} (UUID)."
        )
        raise typer.Exit(1)


def short(value: object, n: int = 12) -> str:
    """The first ``n`` characters of an id, or ``-``."""
    return "-" if value is None else str(value)[:n]


def task_label(body: dict[str, Any]) -> str:
    """``namespace.Name`` from an instance body (``-`` without one)."""
    parts = [str(p) for p in (body.get("__namespace"), body.get("__name")) if p]
    return ".".join(parts) or "-"
