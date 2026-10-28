"""Where a call into stardag comes from: the file whose package settings
(``[tool.stardag]``) apply to it.

The **caller** is the innermost frame outside the ``stardag`` package
(``stardag`` and ``stardag.*`` modules are skipped, so a helper in stardag
calling ``from_store`` on a user's behalf is not the anchor). Its file is
its module's ``__file__``. A frame without one -- a REPL, a notebook cell,
``python -c`` -- has no caller file, and the next anchor (the task class's
defining module), then the working directory, decide instead.

Known limit: a third-party function calling ``from_store`` for user code
(not a decorator or a ``functools.partial``, which add no frame of their
own, nor a lambda, whose frame is the user's module) anchors at that
library's file. Pass ``store=`` there.

An anchor can be pinned for a block with :func:`anchored`: the code a
reference was created in, while it is hydrated at build time.
"""

from __future__ import annotations

import contextvars
import sys
import typing
from contextlib import contextmanager
from pathlib import Path

_ANCHORS: contextvars.ContextVar[tuple[Path | None, ...] | None] = (
    contextvars.ContextVar("stardag_anchors", default=None)
)


def _is_stardag(module_name: object) -> bool:
    return isinstance(module_name, str) and (
        module_name == "stardag" or module_name.startswith("stardag.")
    )


def caller_file(skip: int = 1) -> Path | None:
    """The file of the innermost non-stardag frame, or ``None`` when it has
    none (REPL, notebook, ``-c``)."""
    frame = sys._getframe(skip)
    while frame is not None and _is_stardag(frame.f_globals.get("__name__")):
        frame = frame.f_back
    if frame is None:
        return None
    file = frame.f_globals.get("__file__")
    if not isinstance(file, str) or not file or file.startswith("<"):
        return None
    path = Path(file)
    return path.absolute() if path.exists() else None


def module_file(cls: type) -> Path | None:
    """The file of the module defining ``cls``, when it has one."""
    module = sys.modules.get(cls.__module__)
    file = getattr(module, "__file__", None)
    if not isinstance(file, str) or not file:
        return None
    path = Path(file)
    return path.absolute() if path.exists() else None


def call_anchors(*classes: type | None) -> tuple[Path | None, ...]:
    """The anchors for a call made now: those pinned by :func:`anchored`,
    else the caller's file; then each class's module file."""
    pinned = _ANCHORS.get()
    first = pinned if pinned is not None else (caller_file(2),)
    return (*first, *(module_file(c) for c in classes if c is not None))


@contextmanager
def anchored(*anchors: Path | None) -> typing.Iterator[None]:
    """Treat calls in the block as made from ``anchors``."""
    token = _ANCHORS.set(tuple(anchors))
    try:
        yield
    finally:
        _ANCHORS.reset(token)
