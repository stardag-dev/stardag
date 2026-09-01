"""The app every scenario but one runs on.

A thin module on purpose: ``stardag modal deploy -m <module>`` deploys the
``StardagApp`` it finds at module level, so each deployable app needs its
own module even though the two here are built from the same factory. See
``_scenario_app`` for what the app is and why there are two.
"""

from __future__ import annotations

from ._scenario_app import build_scenario_app

APP_NAME = "registry-live-dag"

app = build_scenario_app(APP_NAME)
