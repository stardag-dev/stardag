"""``stardag build``: build (or trigger) the tasks named by ``module:attr``
references (STA-70, registry v2).

Without ``--app`` it runs ``sd.build`` in this process; with ``--app`` it
calls ``build_trigger`` on that ``StardagApp``, which mints the build here
and spawns the deployed function that drives it. ``--dry-run`` discovers
the DAG locally and prints what a build would plan, writing nothing.
"""

import asyncio
import os
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator, Optional
from uuid import UUID

import typer
from rich.table import Table

from stardag import BaseTask
from stardag._cli._output import JSON_OPTION, emit_json, parse_uuid
from stardag._cli._registry_ctx import console, error_console
from stardag._cli._roots import RefError, parse_params, resolve_app, resolve_roots
from stardag.build._settings import SettingsError, resolve_settings, validate_settings
from stardag.exceptions import StardagError
from stardag.registry import registry_provider


def _parse_settings(pairs: list[str]) -> dict[str, str] | None:
    """``KEY=VALUE`` pairs, validated as the trigger validates them
    (``STARDAG_*`` / ``MODAL_*`` refused). None when none were given, so a
    resume keeps the build's own settings."""
    if not pairs:
        return None
    settings: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SettingsError(f"--settings {pair!r} is not KEY=VALUE")
        settings[key] = value
    return validate_settings(settings)


@contextmanager
def _profile(profile: str | None) -> Iterator[None]:
    """Run with ``STARDAG_PROFILE`` set to ``profile`` (restored after)."""
    if not profile:
        yield
        return
    from stardag.config.loader import clear_config_cache

    old = os.environ.get("STARDAG_PROFILE")
    os.environ["STARDAG_PROFILE"] = profile
    clear_config_cache()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("STARDAG_PROFILE", None)
        else:
            os.environ["STARDAG_PROFILE"] = old
        clear_config_cache()


def _dry_run(
    roots: list[BaseTask], settings: dict[str, str] | None, json_output: bool
) -> None:
    """Discover locally, as a build's static phase would, and print it."""
    from stardag.build._registration import walk_aio
    from stardag.utils.env import temp_env_vars

    with temp_env_vars(settings or {}):
        walk = asyncio.run(walk_aio(roots))
    root_ids = {r.id for r in roots}
    rows: list[dict[str, Any]] = [
        {
            "task_id": str(t.id),
            "task": f"{t.get_namespace()}.{t.get_name()}".lstrip("."),
            "complete": walk.complete[t.id],
            "expanded": t.id in walk.deps,
            "upstreams": [str(d.id) for d in walk.deps.get(t.id, [])],
            "is_root": t.id in root_ids,
        }
        for t in walk.order
    ]
    todo = sum(1 for r in rows if not r["complete"])
    if json_output:
        emit_json(
            {
                "roots": [str(r.id) for r in roots],
                "settings": settings or {},
                "tasks": rows,
                "to_run": todo,
                "previously_completed": len(rows) - todo,
            }
        )
        return
    table = Table(title="Plan (post-order: upstreams first)")
    for col in ("Task ID", "Task", "Complete", "Upstreams", "Root"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["task_id"],
            r["task"],
            "yes" if r["complete"] else "no",
            str(len(r["upstreams"])) if r["expanded"] else "- (not expanded)",
            "yes" if r["is_root"] else "",
        )
    console.print(table)
    console.print(
        f"{len(rows)} task(s): {todo} to run, {len(rows) - todo} already "
        "complete. Dry run — no build was created and nothing ran."
    )


def build_command(
    refs: list[str] = typer.Argument(
        ...,
        help="Root reference(s), 'module:attr' or 'path/file.py:attr': a task "
        "object, a list of them, a zero-argument callable returning either, or "
        "a task class (with --param).",
    ),
    param: list[str] = typer.Option(
        [], "--param", help="KEY=VALUE for a task-class root (repeatable; JSON values)."
    ),
    settings: list[str] = typer.Option(
        [],
        "--settings",
        help="KEY=VALUE environment variable for every process of the build "
        "(repeatable). May change structure and execution, never output. "
        "STARDAG_* and MODAL_* keys are refused.",
    ),
    app_ref: Optional[str] = typer.Option(
        None,
        "--app",
        help="Trigger on this deployed StardagApp ('module:attr', as for "
        "'stardag modal deploy') instead of building here.",
    ),
    reactive: bool = typer.Option(
        False, "--reactive", help="Schedule with short-lived ticks (needs --app)."
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume", help="Resume this build (same roots) instead of a new one."
    ),
    description: Optional[str] = typer.Option(None, "--description"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Discover locally and print the plan; write nothing."
    ),
    stardag_profile: Optional[str] = typer.Option(
        None, "-p", "--stardag-profile", help="Stardag profile (default: active)."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Build the tasks named by REFS (module:attr), here or on a deployed app.

    Without --app: runs ``sd.build`` in this process against the configured
    registry (or none), which creates the build, plans it under this
    code's local deployment and runs it. With --app: calls
    ``build_trigger`` — creates (or resumes) the build and spawns the app's
    ``build`` function, or its reactive ``bootstrap`` with --reactive.
    Prints the build id and a one-line status.

    --dry-run reads the tasks' targets (completion checks) and writes
    nothing: no registry call, no run.
    """
    if reactive and app_ref is None:
        error_console.print(
            "[bold red]Error:[/bold red] --reactive needs --app: reactive "
            "scheduling runs on a deployed app's ticks."
        )
        raise typer.Exit(1)
    resume_id: UUID | None = parse_uuid(resume) if resume else None

    with _profile(stardag_profile):
        try:
            checked = _parse_settings(settings)
        except SettingsError as e:
            error_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(1)

        # A root factory (module:attr callable) may read the environment,
        # so the build's settings must be installed before resolve_roots
        # imports and constructs the roots -- not only later, inside the
        # build itself (sd.build/build_trigger apply them again there;
        # nesting under the same, already-validated settings is a no-op
        # re-entry, see resident_settings). Explicit --settings always
        # wins. A bare resume (no --settings given, checked is None) has
        # nothing new to validate, but still needs its *stored* settings
        # installed now -- resolved from the registry the same way
        # sd.build/build_trigger resolve them later -- or a root factory
        # that reads the environment would construct roots under the
        # ambient environment instead of the resumed build's own.
        from stardag.build._settings import resident_settings

        to_install = checked
        if checked is None and resume_id is not None:
            try:
                to_install = resolve_settings(registry_provider.get(), resume_id, None)
            except SettingsError as e:
                error_console.print(f"[bold red]Error:[/bold red] {e}")
                raise typer.Exit(1)
            except StardagError as e:
                error_console.print(
                    f"[bold red]Error:[/bold red] Could not read build "
                    f"{resume_id}'s settings: {e}"
                )
                raise typer.Exit(1)

        settings_ctx = (
            resident_settings(to_install) if to_install is not None else nullcontext()
        )
        try:
            with settings_ctx:
                try:
                    roots = resolve_roots(refs, parse_params(param))
                except RefError as e:
                    error_console.print(f"[bold red]Error:[/bold red] {e}")
                    raise typer.Exit(1)

                if dry_run:
                    _dry_run(roots, checked, json_output)
                    return
                if app_ref is not None:
                    _trigger(
                        app_ref,
                        roots,
                        checked,
                        resume_id,
                        reactive,
                        description,
                        json_output,
                    )
                else:
                    _build_here(roots, checked, resume_id, description, json_output)
        except SettingsError as e:
            error_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(1)


def _build_here(
    roots: list[BaseTask],
    settings: dict[str, str] | None,
    resume_id: UUID | None,
    description: str | None,
    json_output: bool,
) -> None:
    import stardag as sd

    summary = sd.build(
        roots,
        settings=settings,
        resume_build_id=resume_id,
        description=description,
    )
    counts = summary.task_count
    payload = {
        "build_id": str(summary.build_id) if summary.build_id else None,
        "status": str(summary.status),
        "task_count": {
            "discovered": counts.discovered,
            "previously_completed": counts.previously_completed,
            "succeeded": counts.succeeded,
            "failed": counts.failed,
            "cancelled": counts.cancelled,
            "skipped": counts.skipped,
        },
        "error": str(summary.error) if summary.error else None,
    }
    if json_output:
        emit_json(payload)
    else:
        console.print(
            f"Build {payload['build_id'] or '(no registry)'}: {summary.status} — "
            f"{counts.succeeded} succeeded, {counts.failed} failed, "
            f"{counts.previously_completed} already complete, "
            f"{counts.skipped} skipped."
        )
    if summary.status == "failure":
        if summary.error and not json_output:
            error_console.print(f"[bold red]Error:[/bold red] {summary.error}")
        raise typer.Exit(1)


def _trigger(
    app_ref: str,
    roots: list[BaseTask],
    settings: dict[str, str] | None,
    resume_id: UUID | None,
    reactive: bool,
    description: str | None,
    json_output: bool,
) -> None:
    try:
        stardag_app = resolve_app(app_ref)
    except (RefError, ImportError) as e:
        error_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
    result = stardag_app.build_trigger(
        roots,
        build_id=resume_id,
        description=description,
        reactive=reactive,
        settings=settings,
    )
    call_id = getattr(result.function_call, "object_id", None)
    mode = "reactive" if reactive else "resident"
    if json_output:
        emit_json(
            {
                "build_id": str(result.build_id),
                "app": stardag_app.name,
                "mode": mode,
                "function_call_id": call_id,
                "resumed": resume_id is not None,
            }
        )
        return
    verb = "Resumed" if resume_id is not None else "Triggered"
    console.print(
        f"Build {result.build_id}: {verb} ({mode}) on app {stardag_app.name}"
        + (f", call {call_id}" if call_id else "")
        + f". Follow it with 'stardag builds show {result.build_id}'."
    )
