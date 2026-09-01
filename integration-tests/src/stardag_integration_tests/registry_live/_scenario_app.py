"""The ``StardagApp`` the registry-live scenarios execute on.

Deployed into the run's own Modal environment, alongside the registry it
reports to. Its workers pick the API key up from the ``stardag-api-key``
Modal secret that the connect flow pushed into that same environment -- so
unlike the ``modal_live`` tier, where every registry is a NoOp stand-in,
these workers really do talk to a registry over the network. That is the
whole subject of this tier.

**The interpreter that imports this module is load-bearing.** The image's
Python is derived from it below and the app's functions are serialized, so
whatever deploys an app and whatever triggers a build afterwards must agree
on the Python minor version. They need not be the same checkout -- the local
SDK only mints the build and spawns the bootstrap, and every tick after that
runs in Modal against the deployed image. A mismatch is not a graceful
error: the container dies with ``Runner segmentation fault (SIGSEGV), exit
code: 139``, no traceback, and leaves a build RUNNING with no tasks and
``reactive_app_name`` still null, because the reactive marker is written
last. In the UI that reads as "the build is empty". The line printed on
import puts the version where anyone reading the output will see it.

**Why this is a factory rather than one module-level app.** Two apps are
deployed from it, differing only in name -- see ``watchdog_app``. The
sweep the watchdog scenario drives lists running builds scoped by
*reactive app name*, so it would otherwise reach every other scenario's
builds in the shared environment and wake the dormant ones, which is
exactly the "nothing else could have woken it" that four other scenarios
rest on. A second app name is the whole isolation, and it is cheap: the
image is identical, so Modal reuses its layers.
"""

from __future__ import annotations

import sys

import modal

import stardag.integration.modal as sd_modal

from .selectors import registry_live_limit_keys

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

# The deployed ``tick`` function's own Modal timeout. Named here so a
# scenario can read it rather than restate it -- see ``MAX_LINGER_SECONDS``.
TICK_TIMEOUT_SECONDS = 300

# The longest linger a tick can actually honour, and the reason a scenario
# must not simply ask for the timeout itself.
#
# A linger is a deadline the tick body watches; the Modal timeout is a
# deadline the *container* is killed on, and the container's clock starts
# first -- before the image loads, before the tick body runs. So the two are
# not a tie at equal values: Modal always wins. A tick that reaches a linger
# equal to its timeout is killed rather than exiting through
# ``lingered_out``, which means it writes no summary and performs no exit
# hand-off. In this tier that is unrecoverable, because there is no watchdog
# to spawn a replacement: a build that genuinely stalls hangs to the pytest
# timeout and reports it as the tick having died mid-report, which points
# at the wrong thing entirely.
#
# The margin is generous because it is covering a container start, and
# nothing is paid for it: a tick exits as soon as its build goes terminal,
# so a linger is an upper bound that a passing run never reaches.
MAX_LINGER_SECONDS = TICK_TIMEOUT_SECONDS - 60


def build_scenario_app(app_name: str) -> sd_modal.StardagApp:
    """A scenario app under ``app_name``. Identical but for the name."""
    return sd_modal.StardagApp(
        app_name,
        builder_settings=sd_modal.FunctionSettings(image=image, timeout=900),
        worker_settings={
            "default": sd_modal.FunctionSettings(image=image, timeout=600),
        },
        tick_settings=sd_modal.FunctionSettings(
            image=image, timeout=TICK_TIMEOUT_SECONDS
        ),
        # **Off on both apps, and the scenarios depend on it being off.**
        # The watchdog is a backstop that sweeps builds periodically; with
        # it running, a build that completes proves only that *something*
        # eventually noticed. The question this tier asks is whether the
        # worker itself spawned the next tick, and the only way to ask it
        # is to remove every other thing that could have.
        #
        # It is off on the watchdog app too. That app exists to have a
        # sweep driven at a moment of the scenario's choosing, against a
        # population it controls -- which a periodic sweep would spoil in
        # the same way, by having already swept.
        watchdog_period_minutes=None,
        # A tick must be able to rebuild these from registry data alone,
        # having imported only what the app declared -- see the tasks
        # module.
        task_modules=["stardag_integration_tests.registry_live.tasks"],
        require_pickle_free=True,
        limit_key_selector=registry_live_limit_keys,
    )
