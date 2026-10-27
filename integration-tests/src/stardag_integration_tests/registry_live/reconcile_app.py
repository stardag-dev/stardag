"""A fourth app, owning the reconciliation scenarios' builds and their faults.

Two reasons it is not ``dag_app``, both of which would hold on their own:

* **Its containers carry faults.** ``container_setup`` installs
  ``faults.install_reconcile_faults``: a tick that dies between claiming one
  task class and spawning it, a worker that never reports the end of
  another, and one that never reports a third's failed attempt. Each acts only on its own task class, but a fault is still not
  something to ship into the app every other scenario runs on.
* **Its recovery may be a watchdog sweep.** Both faults leave a claim held
  by something dead and write no flag, so once no tick is alive to
  reconcile it, the time-based wake-up is the watchdog's -- and a sweep is
  scoped by reactive app name. For the reason ``lapse_app`` exists, the
  sweep must reach these scenarios' builds and no one else's.

It also declares one more worker, ``RETRYING_WORKER``, with Modal
``retries`` set: a failed attempt is retried under the same call, which is
the case reconciliation's probe has to read as still running (STA-146).
No other app needs it, and no task routes there unless it asks.

One more deploy of an image that is already built.
"""

from __future__ import annotations

import modal

from ._scenario_app import build_scenario_app
from .faults import install_reconcile_faults
from .selectors import RETRYING_WORKER

APP_NAME = "registry-live-reconcile"

# One retry is enough: the question is what a probe reads after a failed
# attempt -- between it and the next one, and while the next one runs. The
# delay is Modal's backoff before the retry, stretched from its default of
# a second so the gap is wide enough to probe several times.
RETRY_DELAY_SECONDS = 20.0
RETRYING_WORKER_RETRIES = modal.Retries(
    max_retries=1, initial_delay=RETRY_DELAY_SECONDS, backoff_coefficient=1.0
)

app = build_scenario_app(
    APP_NAME,
    container_setup=install_reconcile_faults,
    retrying_workers={RETRYING_WORKER: RETRYING_WORKER_RETRIES},
)
