"""``transition_task`` is the only way a task's ``latest_status`` moves.

Five paths used to pair ``_apply_event_to_task`` with the post-transition
hooks by hand — the event routes, skip-blocked, cascade cancel, the lock's
completion release, and registration. When the cross-build wake-up hook was
added, two of them were missed, so skip-blocked and the lock release flagged
nobody. ``tests/test_wakeups.py`` is the behavioural regression net for
that; this module guards the structure that makes the next hook a one-line
change instead of a search.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "stardag_api"

# The module that owns the transition. Everything else must go through it.
_OWNER = SRC / "services" / "status.py"


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if p != _OWNER)


def test_nothing_outside_status_applies_an_event_to_a_task():
    """No route or service may call ``_apply_event_to_task`` itself.

    Calling it directly is exactly the bug this guards: it moves the status
    and skips every post-transition hook, silently and without failing a
    test that only checks the status came out right.
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name == "_apply_event_to_task":
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "_apply_event_to_task":
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert not offenders, (
        "these call or import _apply_event_to_task directly, bypassing the "
        "post-transition hooks (use services.status.transition_task): "
        + ", ".join(offenders)
    )


def test_the_wake_up_hook_has_exactly_one_call_site():
    """``flag_after_task_transition`` is called from ``transition_task``.

    A second call site means a path that transitions a task without going
    through the helper — the shape the helper exists to prevent — or a
    double flag.
    """
    call_sites: list[str] = []
    for path in [*_python_files(), _OWNER]:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "flag_after_task_transition"
            ):
                call_sites.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert len(call_sites) == 1, (
        "expected exactly one caller of flag_after_task_transition "
        f"(services/status.py's transition_task), found: {call_sites}"
    )
    assert call_sites[0].startswith("services/status.py"), call_sites
