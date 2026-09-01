"""A second app, whose only purpose is to own the watchdog scenario's builds.

The sweep under test lists running builds **scoped by reactive app name**
and spawns a tick for each. That scoping is what makes a separate app
necessary rather than merely tidy: every scenario in this tier runs
concurrently in one Modal environment, so a sweep driven against the shared
app would spawn ticks for whatever else happened to be running at that
moment -- waking the dormant builds that four other scenarios assert
*cannot* be woken by anything but the mechanism they are testing. The
scenarios would not fail; they would quietly stop meaning anything, which is
the worse outcome.

Separating by app name rather than by Modal environment is deliberate. The
environment is this tier's unit of teardown -- one ``modal environment
delete`` takes the registry, its database, the apps, the volume and the
secret together -- and a second environment would need its own registry to
report to, which is the expensive half of the stack. Two apps against one
registry cost an extra deploy of an image that is already built.
"""

from __future__ import annotations

from ._scenario_app import build_scenario_app

APP_NAME = "registry-live-watchdog"

app = build_scenario_app(APP_NAME)
