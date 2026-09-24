"""Resolving ``module:attr`` references for ``stardag build`` (STA-70).

A reference names a module (an import path, or a ``.py`` file) and an
attribute in it, separated by ``:`` (``::`` is accepted too, as
``stardag modal deploy`` spells it). A **root** reference resolves to:

- a task object, or a list/tuple of them;
- a zero-argument callable returning either;
- a task class, instantiated with the ``--param key=value`` arguments
  (each value parsed as JSON when it parses, else taken as a string, then
  validated by the class).

An **app** reference resolves to a ``StardagApp``.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence

from stardag import BaseTask


class RefError(ValueError):
    """A reference that does not resolve to what it must."""


def split_ref(ref: str) -> tuple[str, str]:
    """``(module_or_file, attribute)``; the attribute may be empty."""
    if "::" in ref:
        module, attr = ref.split("::", 1)
    elif ":" in ref:
        module, attr = ref.rsplit(":", 1)
    else:
        module, attr = ref, ""
    if not module:
        raise RefError(f"{ref!r} names no module")
    return module, attr


def import_module_or_file(target: str) -> ModuleType:
    """Import an import path, or execute a ``.py`` file as a module."""
    if "" not in sys.path:
        sys.path.insert(0, "")
    if not target.endswith(".py"):
        try:
            return importlib.import_module(target)
        except ImportError as e:
            raise RefError(f"cannot import {target!r}: {e}") from e
    path = Path(target).resolve()
    if not path.is_file():
        raise RefError(f"no such file: {target}")
    name = inspect.getmodulename(str(path))
    if name is None:
        raise RefError(f"cannot derive a module name from {target!r}")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RefError(f"cannot load {target!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _lookup(module: ModuleType, attr: str, ref: str) -> Any:
    obj: Any = module
    for part in attr.split("."):
        if not hasattr(obj, part):
            raise RefError(f"{ref!r}: {part!r} not found")
        obj = getattr(obj, part)
    return obj


def parse_params(pairs: Sequence[str]) -> dict[str, Any]:
    """``key=value`` pairs; each value JSON-decoded when it decodes."""
    params: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise RefError(f"--param {pair!r} is not KEY=VALUE")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def _as_tasks(value: Any, ref: str) -> list[BaseTask]:
    if isinstance(value, BaseTask):
        return [value]
    if isinstance(value, (list, tuple)) and value:
        if all(isinstance(v, BaseTask) for v in value):
            return list(value)
    raise RefError(
        f"{ref!r} resolved to {type(value).__name__}, not a task or a "
        "non-empty list of tasks"
    )


def resolve_roots(refs: Sequence[str], params: Mapping[str, Any]) -> list[BaseTask]:
    """The root task objects named by ``refs`` (see the module docstring)."""
    roots: list[BaseTask] = []
    used_params = False
    for ref in refs:
        module_name, attr = split_ref(ref)
        if not attr:
            raise RefError(f"{ref!r} names no attribute (use module:attr)")
        obj = _lookup(import_module_or_file(module_name), attr, ref)
        if isinstance(obj, type) and issubclass(obj, BaseTask):
            try:
                roots.append(obj(**params))
            except Exception as e:
                raise RefError(f"cannot construct {ref!r}: {e}") from e
            used_params = True
        elif isinstance(obj, (BaseTask, list, tuple)):
            roots.extend(_as_tasks(obj, ref))
        elif callable(obj):
            try:
                value = obj()
            except Exception as e:
                raise RefError(f"calling {ref!r} raised {type(e).__name__}: {e}") from e
            roots.extend(_as_tasks(value, ref))
        else:
            roots.extend(_as_tasks(obj, ref))
    if params and not used_params:
        raise RefError("--param applies only to a reference naming a task class")
    return roots


def resolve_app(ref: str) -> Any:
    """The ``StardagApp`` named by ``ref`` (the only one in the module when
    no attribute is given)."""
    from stardag.integration.modal import StardagApp

    module_name, attr = split_ref(ref)
    module = import_module_or_file(module_name)
    if attr:
        obj = _lookup(module, attr, ref)
        if not isinstance(obj, StardagApp):
            raise RefError(f"{ref!r} is a {type(obj).__name__}, not a StardagApp")
        return obj
    apps = [o for _, o in inspect.getmembers(module) if isinstance(o, StardagApp)]
    if len(apps) != 1:
        raise RefError(
            f"{module_name!r} holds {len(apps)} StardagApp instances; name one "
            "with module:attr"
        )
    return apps[0]
