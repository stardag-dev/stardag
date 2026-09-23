# v2: deleted in I7
"""v1's build config and its ContextVar transport — a shim until I7.

v2 removed the mechanism this module fed: ``StardagField(significance=...)``
is gone, a ``significant=False`` field is an ordinary parameter passed at
init and stored in the instance body, and nothing reads a field value from
the config any more. What is left is only what the engines still import —
the ContextVar, its JSON coercion, the structure-scope hash and the rebind —
so the package imports and the engines run unchanged until I7 deletes them
together with this module (design: ``docs/design/registry-v2/design.md``,
"Two hashes, one flag").
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from stardag.exceptions import StardagError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from stardag._core.base_task import BaseTask

BuildConfig = Mapping[str, Mapping[str, Any]]
"""``{"<namespace>.<Name>": {"<field>": value}}``. A task in the root
namespace is keyed by its bare class name — see :func:`task_config_key`."""


class BuildConfigError(StardagError):
    """The build config names something the task classes do not have."""


class UnknownTaskClassError(BuildConfigError):
    """The build config names a task class this process has not registered.

    Separate from the other config errors because it is the one a caller
    may legitimately be unable to judge: a trigger process that never
    imported the module holding a configured upstream cannot tell a typo
    from an unimported class, while the bootstrap — which imports every
    task module — can. A misspelled field, a significant field or an invalid
    value is a plain :class:`BuildConfigError` wherever it is seen.
    """


_build_config: ContextVar[BuildConfig | None] = ContextVar(
    "stardag_build_config", default=None
)


def task_config_key(namespace: str, name: str) -> str:
    """The build-config key for a task class: ``namespace.Name``, or ``Name``
    for the root namespace."""
    return f"{namespace}.{name}" if namespace else name


def get_build_config() -> BuildConfig | None:
    """The build config installed for the current context, or None."""
    return _build_config.get()


def set_build_config(config: BuildConfig | None) -> None:
    """Install ``config`` for the current context (a process-wide setting in
    a plain script, a per-task setting under asyncio)."""
    _build_config.set(config)


@contextmanager
def build_config_scope(config: BuildConfig | None) -> Iterator[None]:
    """Install ``config`` for the duration of the block, restoring afterwards.

    What ``sd.build`` does around a build, and what a test does around the
    construction of a task whose level 2 or 3 values it wants to control::

        with sd.build_config_scope({"my_ns.MyTask": {"num_threads": 2}}):
            task = MyTask(param="x")   # num_threads == 2
    """
    token = _build_config.set(config)
    try:
        yield
    finally:
        _build_config.reset(token)


def jsonable_build_config(
    config: BuildConfig | None,
) -> dict[str, dict[str, Any]] | None:
    """``config`` with every value in JSON mode — the form a build config is
    stored and transported in.

    A caller may build the mapping from Python objects (a ``datetime``, a
    ``UUID``, a ``Path``) and each field's validator accepts those, but the
    config is stored with the build and sent to every worker as JSON, so it
    has to *be* JSON before it leaves the caller's process. Validation later
    turns the JSON form back into the field's type, the same way an identity
    parameter round-trips through the registry. A value with no JSON form is
    a :class:`BuildConfigError` here rather than a ``TypeError`` from deep in
    a client library. ``None`` and ``{}`` pass through unchanged.
    """
    if config is None:
        return None
    from pydantic_core import PydanticSerializationError, to_jsonable_python

    out: dict[str, dict[str, Any]] = {}
    for key, fields in config.items():
        out[key] = {}
        for field_name, value in fields.items():
            try:
                out[key][field_name] = to_jsonable_python(value)
            except PydanticSerializationError as e:
                raise BuildConfigError(
                    f"build_config value for {key}.{field_name} "
                    f"({type(value).__name__}) has no JSON form; the config is "
                    "stored with the build and sent to every worker as JSON. "
                    f"Pass a JSON-compatible value instead: {e}"
                ) from e
    return out


def structure_config_hash(config: BuildConfig | None) -> str:
    """Hash of the validated overrides of ``config`` (see
    :func:`canonical_structure_config`): the second half of a v1 structure
    scope key. The classes must be importable here."""
    significant = canonical_structure_config(config)
    payload = json.dumps(significant, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def canonical_structure_config(config: BuildConfig | None) -> dict[str, dict[str, Any]]:
    """The overrides of ``config`` for ``significant=False`` fields, validated
    and serialised in hash mode, defaults dropped. See
    :func:`structure_config_hash`. An override for an unknown task class is
    :class:`UnknownTaskClassError`; for an unknown or significant field,
    :class:`BuildConfigError`."""
    if not config:
        return {}
    # Local imports: this module sits below the task classes.
    from pydantic import TypeAdapter

    from stardag._core.base_task import BaseTask
    from stardag.base_model import CONTEXT_MODE_KEY, is_significant
    from stardag.polymorphic import TypeId

    out: dict[str, dict[str, Any]] = {}
    for key, fields in config.items():
        namespace, _, name = key.rpartition(".")
        try:
            cls: type[BaseModel] = BaseTask._registry().get_class(
                TypeId(namespace=namespace, name=name)
            )
        except KeyError as e:
            raise UnknownTaskClassError(
                f"build_config names {key!r}, which is not registered as a "
                "task class (is its module imported here?)."
            ) from e
        for field_name, value in fields.items():
            field = cls.model_fields.get(field_name)
            if field is None:
                raise BuildConfigError(
                    f"build_config names field {field_name!r} on {key!r}, "
                    f"which has no such field."
                )
            if is_significant(field):
                raise BuildConfigError(
                    f"build_config sets {key}.{field_name}, a significant "
                    "parameter; only significant=False fields may be named."
                )
            adapter = TypeAdapter(field.rebuild_annotation())
            try:
                validated = adapter.validate_python(value)
            except Exception as e:
                raise BuildConfigError(
                    f"build_config value for {key}.{field_name} is not a "
                    f"valid {field.annotation}: {e}"
                ) from e
            dumped = adapter.dump_python(
                validated, mode="json", context={CONTEXT_MODE_KEY: "hash"}
            )
            if not field.is_required():
                default = field.get_default(call_default_factory=True)
                if (
                    adapter.dump_python(
                        default, mode="json", context={CONTEXT_MODE_KEY: "hash"}
                    )
                    == dumped
                ):
                    continue
            out.setdefault(key, {})[field_name] = dumped
    return out


def rebind_to_build_config(task: "BaseTask") -> "BaseTask":
    """Re-create ``task`` through its instance body (a registry-mode dump and
    a compat validation). Since v2 the body carries every field, so this no
    longer resolves anything from the config; it survives only because the
    engines still call it."""
    from stardag.base_model import CONTEXT_MODE_KEY

    data = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
    return type(task).model_validate(data, context={CONTEXT_MODE_KEY: "compat"})
