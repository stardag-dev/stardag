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
"""

from __future__ import annotations

import os

from ._scenario_app import build_scenario_app, image, scenario_image
from .tasks import ROOT_VARIANT_ENV

ROLLOVER_APP_NAME_ENV = "REGISTRY_LIVE_ROLLOVER_APP_NAME"

APP_NAME = os.environ.get(ROLLOVER_APP_NAME_ENV, "") or "registry-live-rollover"

_variant = os.environ.get(ROOT_VARIANT_ENV, "")

app = build_scenario_app(
    APP_NAME,
    app_image=scenario_image({ROOT_VARIANT_ENV: _variant}) if _variant else image,
)
