"""A fourth app, owning the reconciliation scenarios' builds and their faults.

Two reasons it is not ``dag_app``, both of which would hold on their own:

* **Its containers carry faults.** ``container_setup`` installs
  ``faults.install_reconcile_faults``: a tick that dies between claiming one
  task class and spawning it, and a worker that never reports the end of
  another. Each acts only on its own task class, but a fault is still not
  something to ship into the app every other scenario runs on.
* **Its recovery may be a watchdog sweep.** Both faults leave a claim held
  by something dead and write no flag, so once no tick is alive to
  reconcile it, the time-based wake-up is the watchdog's -- and a sweep is
  scoped by reactive app name. For the reason ``lapse_app`` exists, the
  sweep must reach these scenarios' builds and no one else's.

One more deploy of an image that is already built.
"""

from __future__ import annotations

from ._scenario_app import build_scenario_app
from .faults import install_reconcile_faults

APP_NAME = "registry-live-reconcile"

app = build_scenario_app(APP_NAME, container_setup=install_reconcile_faults)
