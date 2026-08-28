"""The app-supplied setup hook every Modal container of an app runs.

Its own module for the same reason :mod:`._logging` is: every entry point
into a Modal container invokes it — the build function, the workers, the
scheduler ticks, the bootstrap and the watchdog — and those live in
different modules of this package.

The hook exists because a ``StardagApp`` registers its functions with
``serialized=True``, so a container unpickles a closure instead of
importing the module the app was declared in. Which of the app's modules
get imported is therefore decided by what each closure happens to
reference — reliable for ``build`` and ``worker_*`` (they close over the
app's ``build_function`` / ``run_function``), and an accident for the
reactive ``tick`` / ``bootstrap`` / ``tick_watchdog``. Passing
``StardagApp(container_setup=...)`` makes it a contract for all five.

That same serialization model is why the placement guardrail lives here
too (:func:`_validate_serialized_callable`): every callable an app hands
``StardagApp`` is pickled into those functions by reference to its
defining module, so where it is *defined* decides whether a container can
hydrate the function at all.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import inspect
import logging
import sys
import threading
import types
import typing

from stardag.exceptions import StardagError

logger = logging.getLogger(__name__)

ContainerSetup = typing.Callable[[], None]
"""Type for an app's container-level setup callable.

Takes no arguments and returns nothing. Run once per container, before
anything else in the Modal function that invoked it — see
``StardagApp(container_setup=...)``.

Like ``worker_selector`` and the app's build/run functions, this is
captured by the serialized Modal functions and so must be defined in a
module that is importable *inside the container* (part of the source added
via ``add_local_python_source(...)``), and imported into the file you
deploy rather than defined there — see
:func:`_validate_serialized_callable`, which refuses the latter. That is
also what makes any module-level code in the hook's own module run in
every container of the app.
"""

# Process-local, and therefore container-local: the closure is unpickled
# per container, but this module is imported from the image like any
# other, so this records exactly "what has this container already set up".
#
# Per *hook* rather than a single flag. A deployed container only ever
# unpickles one app's closure, so in production there is one entry — but a
# process that holds two StardagApps (a test, or a script deploying
# several apps) would otherwise have the first app's hook silence the
# second's, which is a silent wrong answer rather than a missing
# optimisation.
#
# A list compared by identity, not a set: it avoids requiring the hook to
# be hashable, and holding the objects keeps them alive, so an id() cannot
# be recycled onto a different hook after a garbage collection.
_setup_done: list[ContainerSetup] = []
_setup_lock = threading.Lock()


def _validate_container_setup(container_setup: typing.Any) -> None:
    """Reject a hook that cannot be called the way containers will call it.

    Raised where the app is declared rather than surfacing in every
    container of a deployed app. ``callable()`` alone would miss the
    likelier mistake: passing a function that takes an argument, which
    deploys cleanly and then raises ``TypeError`` on every input.

    Signature introspection is best-effort — some callables (C builtins,
    exotic wrappers) have no inspectable signature, and those are let
    through rather than rejected on the strength of a failed guess.
    """
    if not callable(container_setup):
        raise TypeError(
            f"container_setup must be callable, got {type(container_setup).__name__}"
        )
    try:
        signature = inspect.signature(container_setup)
    except (TypeError, ValueError):
        return
    try:
        signature.bind()
    except TypeError as e:
        raise TypeError(
            f"container_setup must be callable with no arguments; "
            f"{getattr(container_setup, '__name__', container_setup)!r} has "
            f"signature {signature}. It is invoked as container_setup() at "
            f"the top of every function the app deploys, with nothing to "
            f"pass it — bind any configuration it needs at the call site "
            f"(a closure or functools.partial)."
        ) from e


# =============================================================================
# Placement: can a container import what it is asked to unpickle?
# =============================================================================


class SerializedCallablePlacementError(StardagError):
    """A callable passed to ``StardagApp`` lives where no container can
    import it.

    Raised at ``StardagApp(...)``, because the deployed app would be
    broken in a way nothing later checks: the deploy succeeds and the
    affected functions then die on every invocation, at hydration, before
    any of their own code runs.
    """


# The name the deploy CLI is currently loading an entry-point *file*
# under. Set only for the duration of that exec (see
# ``_loading_deploy_entrypoint``), which is when an app's ``StardagApp``
# is constructed, and left ``None`` in every other process — including
# every container, which imports this module from the image like any
# other.
#
# It has to be told to us rather than derived. The CLI names the module
# after the file (``modal/app.py`` -> ``app``) and puts the file's
# directory on ``sys.path``, so from inside the process the name resolves:
# ``importlib.util.find_spec("app")`` finds it, in ``sys.modules`` and
# again on disk. Nothing about it looks synthetic locally; what makes it
# unimportable is the container, where that directory is not on the path
# and usually not in the image at all.
_deploy_entrypoint_module: str | None = None


@contextlib.contextmanager
def _loading_deploy_entrypoint(module_name: str) -> typing.Iterator[None]:
    """Record the name a deploy entry-point file is being loaded under.

    Wraps the CLI's ``exec_module`` of the entry point, so any
    ``StardagApp`` constructed while it runs can tell "defined in the
    entry point" from "imported into it".

    Restores the previous value rather than clearing it: a process that
    loads two entry points (a test, a script deploying several apps)
    must not have the first one's name outlive it.
    """
    global _deploy_entrypoint_module
    previous = _deploy_entrypoint_module
    _deploy_entrypoint_module = module_name
    try:
        yield
    finally:
        _deploy_entrypoint_module = previous


def _pickled_by_reference(value: typing.Any) -> tuple[str, str] | None:
    """The ``(module, qualname)`` a container must import to unpickle ``value``.

    ``None`` when there is none — cloudpickle would write ``value`` out by
    value, so the payload is self-contained and no import is needed.

    This mirrors what cloudpickle decides, because that is the thing that
    actually matters, and it differs from "which module was this written
    in" in both directions:

    - A **module-level def**, or the **class of a callable instance**, is
      stored as a bare ``module.qualname`` reference. This is the case
      that breaks, and the common one: a ``def`` in the deploy entry
      point, or a ``Builder``/``Runner`` subclass defined there.
    - A **lambda**, a **closure**, or anything defined in ``__main__`` is
      written out by value — cloudpickle cannot look it up again by name,
      so it serialises the code object. Those work, and rejecting them
      would break apps that are fine today.
    - A ``functools.partial`` or a **bound method** is itself by value but
      wraps something that may not be, so we look through to the wrapped
      callable. ``functools.partial`` matters here in particular: it is
      what an app is told to reach for when a hook needs configuration
      bound to it.

    Best-effort by design. Anything it cannot decide reads as ``None`` and
    is let through, on the same principle as the arity check above: a
    guess is not worth refusing a deploy over.
    """
    if isinstance(value, functools.partial):
        return _pickled_by_reference(value.func)
    if isinstance(value, types.MethodType):
        # A bound method reduces to (getattr, (__self__, name)); it is
        # __self__ — the instance, or the class for a classmethod — that
        # carries the reference.
        return _pickled_by_reference(value.__self__)
    target = value if isinstance(value, (types.FunctionType, type)) else type(value)
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        return None
    if module == "__main__":
        return None
    defining_module = sys.modules.get(module)
    if defining_module is None:
        # __module__ names something not imported in this process. The
        # round-trip below cannot be confirmed, but the name is still what
        # a container would be asked for, so report it and let the
        # caller's importability check speak.
        return module, qualname
    try:
        found = functools.reduce(getattr, qualname.split("."), defining_module)
    except AttributeError:
        # Not reachable under its own name (a lambda, a closure, a
        # dynamically built callable) -> cloudpickle writes it by value.
        return None
    return (module, qualname) if found is target else None


def _validate_serialized_callable(parameter: str, value: typing.Any) -> None:
    """Reject a callable no container of the app could unpickle.

    Everything a ``StardagApp`` is handed — ``container_setup``,
    ``worker_selector``, ``limit_key_selector``, ``build_function``,
    ``run_function`` — is cloudpickled into the ``serialized=True``
    functions ``finalize()`` registers, and cloudpickle stores a
    module-level callable as a *reference* to its defining module. A
    container that cannot import that module cannot hydrate the function,
    and fails before reaching any of the app's own code::

        ModuleNotFoundError: No module named 'app'

    The mistake this catches is defining such a callable in the deploy
    entry point itself. ``stardag modal deploy path/to/app.py`` loads that
    file under the module name ``app``, so the callable pickles as
    ``app.<name>`` — a name that exists only in the deploying process.
    Every deploy-time signal stays green, and the damage shows up later,
    per function: ``build`` and ``worker_*`` often survive (their closures
    reach package modules anyway) while the scheduled reactive functions
    do not.

    Raises rather than warns. Unlike the ``task_modules`` coverage
    warning, there is no degraded-but-working path on the other side of
    this one: the function is guaranteed to fail in every container, on
    every invocation, and a warning would be read at the same moment the
    outage is — which is to say, afterwards.
    """
    reference = _pickled_by_reference(value)
    if reference is None:
        return
    module, qualname = reference
    fix = (
        f"Move {qualname} into a module of your own package — one the "
        f"image adds with add_local_python_source(...) — and import it "
        f"into the entry point instead of defining it there."
    )
    if module == _deploy_entrypoint_module:
        raise SerializedCallablePlacementError(
            f"{parameter} pickles as {module}.{qualname}, and {module!r} is "
            f"the name this deploy loaded the entry-point file under — not a "
            f"module in the app's image. Everything passed to StardagApp is "
            f"cloudpickled into the functions it deploys, and a module-level "
            f"callable is stored as a reference to its defining module, so "
            f"every container that hydrates it would fail with "
            f'"ModuleNotFoundError: No module named {module!r}". {fix}'
        )
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        raise SerializedCallablePlacementError(
            f"{parameter} pickles as {module}.{qualname}, and {module!r} is "
            f"not importable even here. Everything passed to StardagApp is "
            f"cloudpickled into the functions it deploys, and a module-level "
            f"callable is stored as a reference to its defining module, so a "
            f"container could not import it either. {fix}"
        )


def _already_run(container_setup: ContainerSetup) -> bool:
    return any(done is container_setup for done in _setup_done)


def _run_container_setup(container_setup: ContainerSetup | None) -> None:
    """Run ``container_setup`` at most once in this container.

    A no-op when the app supplied no hook.

    Under a lock because a worker with ``allow_concurrent_inputs`` serves
    inputs on threads, and setup that runs twice concurrently is exactly
    what the app is being spared from writing itself.

    Failures propagate and are **not** memoised: the hook is recorded as
    done only on success, so one that raised is attempted again on the
    next input rather than leaving the rest of the container's inputs
    running silently un-set-up. A hook that fails deterministically
    therefore fails every input — the honest outcome when the container is
    not in the state the app requires.

    Note where that failure is visible. Running before the wrapper's own
    body means running before a worker's lifecycle reporter exists, so a
    raising hook surfaces in the Modal logs and *not* as a TASK_FAILED in
    the registry; a reactive build sees the execution claim lapse and the
    next tick re-spawn. That is the same shape as a raising
    ``Runner.setup()`` and is the right layering — the hook is what makes
    the container able to talk to the registry in the first place — but it
    does mean the registry is not where you will see it.
    """
    if container_setup is None or _already_run(container_setup):
        return
    with _setup_lock:
        if _already_run(container_setup):
            return
        logger.debug(f"Running container setup: {container_setup!r}")
        container_setup()
        _setup_done.append(container_setup)


def _reset_container_setup_for_testing() -> None:
    """Forget that setup ran, so a test can exercise a "fresh container"."""
    with _setup_lock:
        _setup_done.clear()
