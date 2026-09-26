"""The app the rollover scenarios deploy -- several times, under several ids.

Its own module because the scenarios redeploy it mid-run, and a redeploy of
the shared ``dag_app`` would roll every other scenario's build over too.
Same factory, same tasks; only the name (and, for S20, one baked variable)
differs. See ``_scenario_app``.

**One app name per scenario.** The rollover scenarios run concurrently in
CI, and a deploy moves *every* running build of its app to the new code, so
two scenarios sharing an app name would roll each other over. The deploying
process names the app through ``ROLLOVER_APP_NAME_ENV`` (read here, at
import, which for ``stardag modal deploy -m`` is deploy time); the
triggering process builds a handle of the same name with
``_rollover.trigger_app`` rather than importing ``app`` from here.

``ROOT_VARIANT_ENV`` is baked into the image for the same reason it is read
at import in ``tasks``: it is how a deploy of *this* module stands in for new
code that changed a root's identity (S20).

Every function scales down within ``ROLLOVER_SCALEDOWN_SECONDS`` of its
last input, so no warm container of an old deploy is left for a new spawn to
land in; and ``deploy_probe`` answers with the nonce its deploy was given, so
the deploying scenario can wait until a fresh container reaches the new code
(see ``_rollover.deploy_rollover_app``). The nonce rides in a secret on that one
function rather than in the image, so a redeploy of unchanged code (S6)
still has an unchanged image.
"""

from __future__ import annotations

import os

import modal

from ._scenario_app import (
    ROLLOVER_SCALEDOWN_SECONDS,
    build_scenario_app,
    image,
    scenario_image,
)
from .tasks import ROOT_VARIANT_ENV

ROLLOVER_APP_NAME_ENV = "REGISTRY_LIVE_ROLLOVER_APP_NAME"
DEPLOY_NONCE_ENV = "REGISTRY_LIVE_DEPLOY_NONCE"
DEPLOY_PROBE_FUNCTION = "deploy_probe"

APP_NAME = os.environ.get(ROLLOVER_APP_NAME_ENV, "") or "registry-live-rollover"

_variant = os.environ.get(ROOT_VARIANT_ENV, "")

_image = scenario_image({ROOT_VARIANT_ENV: _variant}) if _variant else image

app = build_scenario_app(
    APP_NAME, app_image=_image, scaledown_window=ROLLOVER_SCALEDOWN_SECONDS
)

_nonce = os.environ.get(DEPLOY_NONCE_ENV, "")


@app.modal_app.function(
    image=_image,
    secrets=[modal.Secret.from_dict({DEPLOY_NONCE_ENV: _nonce})],
    timeout=60,
    # Short, so a probe never answers from a warm container of an earlier
    # deploy: each call the scenario makes is then a fresh container, and
    # its answer is where a fresh container of *this* app lands now.
    scaledown_window=ROLLOVER_SCALEDOWN_SECONDS,
    name=DEPLOY_PROBE_FUNCTION,
)
def deploy_probe() -> str:
    """This deploy's nonce: which deploy a fresh call actually reached."""
    return os.environ.get(DEPLOY_NONCE_ENV, "")
