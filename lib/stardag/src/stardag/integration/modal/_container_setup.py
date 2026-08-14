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
"""

from __future__ import annotations

import logging
import threading
import typing

logger = logging.getLogger(__name__)

ContainerSetup = typing.Callable[[], None]
"""Type for an app's container-level setup callable.

Takes no arguments and returns nothing. Run once per container, before
anything else in the Modal function that invoked it — see
``StardagApp(container_setup=...)``.

Like ``worker_selector`` and the app's build/run functions, this is
captured by the serialized Modal functions and so must be defined in a
module that is importable *inside the container* (part of the source added
via ``add_local_python_source(...)``, not a loose deploy script). That is
also what makes any module-level code in the hook's own module run in
every container of the app.
"""

# Process-local, and therefore container-local: the closure is unpickled
# per container, but this module is imported from the image like any
# other, so the flag is exactly "has this container run setup yet".
_setup_done = False
_setup_lock = threading.Lock()


def _run_container_setup(container_setup: ContainerSetup | None) -> None:
    """Run ``container_setup`` at most once in this container.

    A no-op when the app supplied no hook.

    Under a lock because a worker with ``allow_concurrent_inputs`` serves
    inputs on threads, and setup that runs twice concurrently is exactly
    what the app is being spared from writing itself.

    Failures propagate and are **not** memoised: the "done" flag is set
    only on success, so a hook that raised is attempted again on the next
    input rather than leaving the rest of the container's inputs running
    silently un-set-up. A hook that fails deterministically therefore
    fails every input, loudly — which is the honest outcome when the
    container is not in the state the app requires.
    """
    global _setup_done
    if container_setup is None or _setup_done:
        return
    with _setup_lock:
        if _setup_done:
            return
        logger.debug(f"Running container setup: {container_setup!r}")
        container_setup()
        _setup_done = True


def _reset_container_setup_for_testing() -> None:
    """Forget that setup ran, so a test can exercise a "fresh container"."""
    global _setup_done
    with _setup_lock:
        _setup_done = False
