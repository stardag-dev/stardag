"""The ``StardagApp`` the registry-live scenarios execute on.

Deployed into the run's own Modal environment, alongside the registry it
reports to. Its workers pick the API key up from the ``stardag-api-key``
Modal secret that the connect flow pushed into that same environment -- so
unlike the ``modal_live`` tier, where every registry is a NoOp stand-in,
these workers really do talk to a registry over the network. That is the
whole subject of this tier.

**The interpreter that imports this module is load-bearing.** The image's
Python is derived from it below and the app's functions are serialized, so
whatever deploys the app and whatever triggers a build afterwards must agree
on the Python minor version. They need not be the same checkout -- the local
SDK only mints the build and spawns the bootstrap, and every tick after that
runs in Modal against the deployed image. A mismatch is not a graceful
error: the container dies with ``Runner segmentation fault (SIGSEGV), exit
code: 139``, no traceback, and leaves a build RUNNING with no tasks and
``reactive_app_name`` still null, because the reactive marker is written
last. In the UI that reads as "the build is empty". The line printed on
import puts the version where anyone reading the output will see it.
"""

from __future__ import annotations

import sys

import modal

import stardag.integration.modal as sd_modal

from .selectors import registry_live_limit_keys

APP_NAME = "registry-live-dag"

python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

print(
    f"[registry-live] python {python_version} "
    "-- deploys and triggers must agree on this",
    file=sys.stderr,
)

# Installs the *local* checkout when the SDK reports a dev version, which is
# what makes this an end-to-end test of the branch rather than of the last
# PyPI release.
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version=python_version)
).add_local_python_source("stardag_integration_tests")

app = sd_modal.StardagApp(
    APP_NAME,
    builder_settings=sd_modal.FunctionSettings(image=image, timeout=900),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image, timeout=600),
    },
    tick_settings=sd_modal.FunctionSettings(image=image, timeout=300),
    # **Off, and the scenarios depend on it being off.** The watchdog is a
    # backstop that sweeps builds periodically; with it running, a build
    # that completes proves only that *something* eventually noticed. The
    # question this tier asks is whether the worker itself spawned the next
    # tick, and the only way to ask it is to remove every other thing that
    # could have.
    watchdog_period_minutes=None,
    # A tick must be able to rebuild these from registry data alone, having
    # imported only what the app declared -- see the tasks module.
    task_modules=["stardag_integration_tests.registry_live.tasks"],
    require_pickle_free=True,
    limit_key_selector=registry_live_limit_keys,
)
