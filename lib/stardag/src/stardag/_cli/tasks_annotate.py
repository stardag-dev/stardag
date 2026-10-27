"""``stardag tasks annotate``: set a task's tags and description (registry
v2). Split from :mod:`stardag._cli.tasks` by the module-size rule; the
command is registered on its ``app``.

Annotations describe the completion (the task id), not one instance of it.
They are mutable and outside every hash, so annotating never changes a task
id. Tags are for finding tasks (``stardag tasks list --tag``); code refers
to a task by its id.
"""

from typing import Optional

import typer
from rich.markup import escape

from stardag._cli._output import JSON_OPTION, emit_json
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError

_TASK_ID = typer.Argument(..., help="Task ID (the completion hash)")


def _usage(message: str) -> None:
    error_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(2)


def tasks_annotate(
    task_id: str = _TASK_ID,
    tag: Optional[list[str]] = typer.Option(
        None,
        "--tag",
        help="Replace the task's tags with these (repeatable).",
    ),
    add_tag: Optional[list[str]] = typer.Option(
        None, "--add-tag", help="Add a tag, keeping the others (repeatable)."
    ),
    remove_tag: Optional[list[str]] = typer.Option(
        None,
        "--remove-tag",
        help="Remove a tag, keeping the others (repeatable).",
    ),
    clear_tags: bool = typer.Option(False, "--clear-tags", help="Remove every tag."),
    description: Optional[str] = typer.Option(
        None,
        "--description",
        "-d",
        help="Set the description ('' clears it).",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Set a task's tags and/or description; what is not named is left as
    it is. ``--tag`` replaces the set, ``--add-tag``/``--remove-tag`` edit
    it, ``--clear-tags`` empties it. The registry trims, deduplicates and
    sorts tags, and refuses an empty or over-long one (64 characters), or
    more than 32.

    Writes ``PATCH /tasks/{id}``; ``--add-tag``/``--remove-tag`` read
    ``GET /tasks/{id}`` first and send the edited set, so a concurrent
    annotation between the two is overwritten.
    """
    replaces = tag is not None or clear_tags
    edits = bool(add_tag or remove_tag)
    if replaces and edits:
        _usage(
            "--tag/--clear-tags replace the tags; do not combine them with "
            "--add-tag/--remove-tag"
        )
    if tag is not None and clear_tags:
        _usage("--tag and --clear-tags contradict each other")
    if not (replaces or edits or description is not None):
        _usage(
            "nothing to change: pass --tag, --add-tag, --remove-tag, "
            "--clear-tags or --description"
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        tags: list[str] | None = None
        if clear_tags:
            tags = []
        elif tag is not None:
            tags = list(tag)
        elif edits:
            current = registry.task_get(task_id).tags
            removed = {t.strip() for t in remove_tag or []}
            tags = [t for t in current if t not in removed] + list(add_tag or [])
        task = registry.task_annotate(
            task_id,
            tags=tags,
            description=... if description is None else description,
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(task.model_dump(mode="json", exclude={"instances"}))
        return
    console.print(f"Annotated task {task.task_id}")
    tags_text = ", ".join(task.tags) if task.tags else "-"
    console.print(f"  Tags:        {escape(tags_text)}")
    console.print(f"  Description: {escape(task.description or '-')}")
