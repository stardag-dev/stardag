"""A third app, owning the builds whose recovery is a *time-based* wake-up.

A lapsed claim writes nothing: no status changes, so no build is flagged,
and a lingering tick -- which polls the wake-up flag, not the frontier --
never looks again. Time-based wake-ups are the watchdog's (design.md,
"What carries over"), so a scenario about a lapse has to drive a sweep, and
a sweep is scoped by reactive app name. On the shared ``dag_app`` it would
wake every other scenario's dormant builds; on ``watchdog_app`` it would
spawn ticks into the population ``test_watchdog_sweep`` counts. Hence an app
of its own, for the same reason and at the same price as the watchdog's: one
more deploy of an image that is already built.
"""

from __future__ import annotations

from ._scenario_app import build_scenario_app

APP_NAME = "registry-live-lapse"

app = build_scenario_app(APP_NAME)
