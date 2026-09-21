"""The build config: the one source of a task's level 2 and 3 parameters.

A task's parameters fall into three levels of *significance* (see
``StardagField``): identity (the task id), dependencies-only (the upstream
set, not the output — a fan-out width) and execution-only (neither — a
thread count). Levels 2 and 3 are never passed at init; they are read from
**the build config**, a mapping ``{"<namespace>.<Name>": {"<field>": value}}``
that is fixed for a build's life and stored on the build in the registry.

Why one mechanism and not two. If a downstream could pass a partition size
to its upstream while another downstream let the upstream read the config,
there would be two versions of one upstream in one build, with one task id,
not separable by anything — and every root would have to expose the level 2
and 3 parameters of its whole upstream cone to keep any cache honest.
Forbidding explicit init removes both problems by construction. See
``docs/design/scope-keyed-dependency-structure.md``.

A key names a **class**, and usually a task class. It may also name a
plain :class:`~stardag.base_model.StardagBaseModel` that declares level 2
or 3 fields of its own — a nested config object held as a task parameter,
where a guard-rail cap or a worker count naturally lives. Such a class is
indexed in a small registry here when it is defined; see
:func:`register_build_config_class`.

The config is *installed* for a build — the trigger, the bootstrap, a tick
and a worker each set it before any task of that build is constructed or
rehydrated — and *read* by a field's default at validation time. It lives in
a context variable so two builds in one process (two ``sd.build`` calls in
one asyncio program) each see their own.
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
namespace is keyed by its bare class name — see :func:`task_config_key`. A
non-task model is keyed the same way: its ``__namespace__`` and class name,
or the bare class name when it has no namespace."""


class BuildConfigError(StardagError):
    """The build config names something the task classes do not have."""


class UnknownTaskClassError(BuildConfigError):
    """The build config names a class this process has not registered.

    A task class, or a non-task model declaring build-config fields — the
    lookup tries both. The name predates the second kind and is kept: it is
    public API, and an unimported upstream task is still the common case.

    Separate from the other config errors because it is the one a caller
    may legitimately be unable to judge: a trigger process that never
    imported the module holding a configured upstream cannot tell a typo
    from an unimported class, while the bootstrap — which imports every
    task module — can. A misspelled field, an identity field or an invalid
    value is a plain :class:`BuildConfigError` wherever it is seen.
    """


_build_config: ContextVar[BuildConfig | None] = ContextVar(
    "stardag_build_config", default=None
)


def task_config_key(namespace: str, name: str) -> str:
    """The build-config key for a task class: ``namespace.Name``, or ``Name``
    for the root namespace."""
    return f"{namespace}.{name}" if namespace else name


_build_config_classes: dict[str, type["BaseModel"]] = {}
"""Non-task models a build config may name, by build-config key.

Task classes are *not* here: the task registry already indexes them, and
:func:`canonical_structure_config` consults it first. This holds the other
half — a plain ``StardagBaseModel`` that declares a ``dependencies_only``
or ``execution_only`` field — so that half can be looked up at all. Without
it a nested config model could resolve its fields at validation and still
fail the moment the build's structure scope was hashed.
"""


def register_build_config_class(cls: type["BaseModel"]) -> None:
    """Index ``cls`` under its build-config key, if a config can name it.

    Called for every ``StardagBaseModel`` subclass as it is defined. Three
    kinds of class are skipped:

    - a parameterised generic alias (``Model[int]``), which is not a real
      class — the concrete subclass that extends it carries the key;
    - a class with no build-config field, which is most of them. Indexing
      every model would re-create the polymorphic family registry for
      classes no config can name, and make every ordinary ``Config``-style
      class a collision candidate. A legacy ``hash_exclude=True`` field
      does not count: it is passable at init and needs no config entry;
    - a task class, which the task registry owns.

    Two classes resolving to one key is a definition-time error naming
    both: a config entry could not say which it meant. Set ``__namespace__``
    on one of them to separate the keys. A class re-created under the same
    module and qualified name — cloudpickle unpickling a by-value class in
    a worker, or a module reloaded — replaces its predecessor rather than
    colliding with it.
    """
    # ``_non_identity_fields`` / ``_build_config_key`` are StardagBaseModel
    # classmethods; this module sits below it, so ``cls`` is typed loosely.
    if cls.__pydantic_generic_metadata__.get("origin"):  # type: ignore[attr-defined]
        return
    if not cls._non_identity_fields():  # type: ignore[attr-defined]
        return
    if _is_task_class(cls):
        return

    key: str = cls._build_config_key()  # type: ignore[attr-defined]
    existing = _build_config_classes.get(key)
    if (
        existing is not None
        and existing is not cls
        and (existing.__module__, existing.__qualname__)
        != (cls.__module__, cls.__qualname__)
    ):
        raise BuildConfigError(
            f"Two models resolve to the build-config key {key!r}: "
            f"{existing.__module__}.{existing.__qualname__} and "
            f"{cls.__module__}.{cls.__qualname__}. A build config names a "
            "class by this key, so it could not say which one it meant. Set "
            "__namespace__ on one of them."
        )
    _build_config_classes[key] = cls


def get_build_config_class(key: str) -> type["BaseModel"] | None:
    """The non-task model registered under ``key``, or None. See
    :data:`_build_config_classes`."""
    return _build_config_classes.get(key)


def _is_task_class(cls: type["BaseModel"]) -> bool:
    """Whether ``cls`` is a task class, whose key the task registry owns."""
    try:
        from stardag._core.base_task import BaseTask
    except ImportError:  # pragma: no cover - only while base_task itself imports
        return False
    return issubclass(cls, BaseTask)


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


def resolve_field_value(key: str, field_name: str) -> tuple[bool, Any]:
    """Look ``field_name`` up for the class keyed ``key`` in the installed
    config. Returns ``(found, value)``."""
    config = _build_config.get()
    if not config:
        return False, None
    entry = config.get(key)
    if not entry or field_name not in entry:
        return False, None
    return True, entry[field_name]


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
    """Hash of the ``dependencies_only`` part of ``config``: the second half
    of a structure scope key.

    Every override is validated against its field's type and serialised in
    **hash mode**, so the hash controls a task class puts on a field — a
    float truncated for stability, a custom hash-mode serializer — apply to
    the config value exactly as to an identity parameter. An override equal
    to the field's default is dropped, so a no-op override is not a new
    scope. ``execution_only`` overrides are validated but excluded: they
    change how work is done, not what is planned. An override for a class or
    field that does not exist, or for an identity field, is a
    :class:`BuildConfigError` — that is the trigger's chance to fail before
    a build with a misspelled key silently runs at the defaults.

    The classes must be importable here: this runs where discovery runs (the
    reactive bootstrap, or a local build), never on the server.
    """
    significant = canonical_structure_config(config)
    payload = json.dumps(significant, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def canonical_structure_config(config: BuildConfig | None) -> dict[str, dict[str, Any]]:
    """The ``dependencies_only`` overrides of ``config``, validated and
    serialised in hash mode, defaults dropped. See :func:`structure_config_hash`."""
    if not config:
        return {}
    # Local imports: this module sits below the task classes.
    from pydantic import TypeAdapter

    from stardag._core.base_task import BaseTask
    from stardag.base_model import CONTEXT_MODE_KEY, field_significance
    from stardag.polymorphic import TypeId

    out: dict[str, dict[str, Any]] = {}
    for key, fields in config.items():
        namespace, _, name = key.rpartition(".")
        try:
            cls: type[BaseModel] = BaseTask._registry().get_class(
                TypeId(namespace=namespace, name=name)
            )
        except KeyError as e:
            # Tasks first, so an ordinary config keeps today's messages; then
            # the non-task models that declare build-config fields of their
            # own — a nested config object is named by the same kind of key.
            model = get_build_config_class(key)
            if model is None:
                raise UnknownTaskClassError(
                    f"build_config names {key!r}, which is not registered as "
                    "a task class, nor as a model declaring build-config "
                    "fields (is its module imported here?)."
                ) from e
            cls = model
        for field_name, value in fields.items():
            field = cls.model_fields.get(field_name)
            if field is None:
                raise BuildConfigError(
                    f"build_config names field {field_name!r} on {key!r}, "
                    f"which has no such field."
                )
            significance = field_significance(field)
            if significance == "identity":
                raise BuildConfigError(
                    f"build_config sets {key}.{field_name}, an identity "
                    "parameter. Identity parameters are passed at init and "
                    "are part of the task id; only dependencies_only and "
                    "execution_only fields are read from the build config."
                )
            # ``rebuild_annotation`` keeps the field's ``Annotated`` metadata,
            # which is where a custom hash-mode serializer lives; the bare
            # annotation would drop it and hash the raw value.
            adapter = TypeAdapter(field.rebuild_annotation())
            try:
                validated = adapter.validate_python(value)
            except Exception as e:
                raise BuildConfigError(
                    f"build_config value for {key}.{field_name} is not a "
                    f"valid {field.annotation}: {e}"
                ) from e
            if significance != "dependencies_only":
                continue
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
    """Re-create ``task`` under the installed build config.

    A task object constructed elsewhere — on the laptop that triggered the
    build, before any config existed — carries the level 2 and 3 values that
    were resolvable there. Rebinding dumps its identity-level data and
    validates it again, so every non-identity field is resolved from the
    config installed *here*. The task id is unchanged by construction: only
    identity fields survive the dump.
    """
    from stardag.base_model import CONTEXT_MODE_KEY

    data = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
    return type(task).model_validate(data, context={CONTEXT_MODE_KEY: "compat"})
