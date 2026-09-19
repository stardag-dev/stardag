"""The app the rollover scenario deploys twice, under two code ids.

Its own module because the scenario redeploys it mid-run — first as code
``A``, then as code ``B`` — and a redeploy of the shared ``dag_app`` would
roll every other scenario's build over too. Same factory, same tasks, only
the name differs; see ``_scenario_app``.
"""

from __future__ import annotations

from ._scenario_app import build_scenario_app

APP_NAME = "registry-live-rollover"

app = build_scenario_app(APP_NAME)
