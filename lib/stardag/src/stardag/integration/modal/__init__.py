"""Stardag on Modal — deploy an app, then run builds on it.

:class:`StardagApp` wraps a ``modal.App`` and registers the functions a
stardag deployment consists of: ``build`` (a resident orchestrator),
``worker_*`` (one per configured worker), and — for reactive scheduling —
``bootstrap``, ``tick`` and an optional ``tick_watchdog``.

Example usage:

    import modal
    from stardag.integration.modal import StardagApp, FunctionSettings

    # Define your image (user has full control)
    image = (
        modal.Image.debian_slim()
        .pip_install("pandas", "numpy", "stardag")
        .add_local_python_source("my_code")  # Local sources last for caching
    )

    # Create the app (functions are NOT created yet)
    stardag_app = StardagApp(
        "my-app",
        builder_settings=FunctionSettings(image=image),
        worker_settings={"default": FunctionSettings(image=image)},
    )

    # Deploy with: stardag modal deploy my_app.py --profile prod
    # The --profile flag injects environment variables as a Modal secret

    # To run tasks remotely (after deployment):
    from my_tasks import my_task
    stardag_app.build_spawn(my_task)  # Looks up deployed function by name

Everything below is private; import from this package rather than from the
modules directly. Roughly in the order a build passes through them:

- :mod:`._profile`, :mod:`._volumes`, :mod:`._settings` — deploy-time
  inputs: profile env vars, target-root volumes, the ``FunctionSettings``
  declaration and the merge applied to it.
- :mod:`._protocols`, :mod:`._selector` — the contracts of the two
  callables an app deploys, and worker routing.
- :mod:`._app` — ``StardagApp`` itself: registering those functions
  (``finalize``) and triggering builds on them.
- :mod:`._builder` — the resident ``build`` function (``Builder``).
- :mod:`._executor` — the orchestrator side (``ModalTaskExecutor``):
  spawning tasks onto workers, re-attaching, cancelling.
- :mod:`._runner` — the worker side (``Runner``): running one task and
  self-reporting its lifecycle.
- :mod:`._bootstrap`, :mod:`._tick` — reactive scheduling: arming a build,
  then driving it one frontier pass at a time.
- :mod:`._metadata` — the Modal coordinates behind the UI's dashboard deep
  links, and the env-var channel that carries them to workers.
- :mod:`._target`, :mod:`._config` — ``modalvol://`` targets and image
  helpers.
"""

from stardag.integration.modal._app import (
    BuildTriggerResult,
    FinalizeResult,
    StardagApp,
)
from stardag.integration.modal._bootstrap import ReactiveDiscovery
from stardag.integration.modal._builder import (
    BuildFailedError,
    Builder,
    PrefectBuilder,
)
from stardag.integration.modal._config import get_package_deps, with_stardag_on_image
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._profile import get_profile_env_vars, get_profile_secret
from stardag.integration.modal._protocols import BuildFunction, RunFunction
from stardag.integration.modal._runner import Runner
from stardag.integration.modal._selector import (
    WorkerSelection,
    WorkerSelector,
    WorkerSelectorByName,
)
from stardag.integration.modal._settings import FunctionSettings
from stardag.integration.modal._target import (
    MODAL_VOLUME_URI_PREFIX,
    VOLUME_MOUNT_PATH_PREFIX,
    ModalMountedVolumeFileTarget,
    get_default_volume_mount_path,
    get_modal_target,
    get_volume_name_and_path,
)

__all__ = [
    "BuildFailedError",
    "BuildFunction",
    "BuildTriggerResult",
    "ReactiveDiscovery",
    "RunFunction",
    "StardagApp",
    "Builder",
    "ModalTaskExecutor",
    "PrefectBuilder",
    "Runner",
    "FinalizeResult",
    "FunctionSettings",
    "MODAL_VOLUME_URI_PREFIX",
    "VOLUME_MOUNT_PATH_PREFIX",
    "ModalMountedVolumeFileTarget",
    "get_default_volume_mount_path",
    "get_volume_name_and_path",
    "get_modal_target",
    "WorkerSelection",
    "WorkerSelector",
    "WorkerSelectorByName",
    "get_profile_env_vars",
    "get_profile_secret",
    "with_stardag_on_image",
    "get_package_deps",
]
