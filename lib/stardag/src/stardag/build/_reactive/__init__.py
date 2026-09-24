"""Reactive (tick-based) build scheduling.

No resident orchestrator lives for the whole build. Short-lived, idempotent
**ticks** drive it instead, each holding the build's scheduler lease
(single-flight) while it loops:

1. clear the wake-up flag and read the **frontier** of the build's active
   plan (the registry runs its closure step first);
2. if the plan belongs to another deployment, **roll over** — re-plan the
   build under this tick's deployment, when that is the app's current one —
   or exit ``superseded``;
3. act on the frontier under the plan's **settings**: expand the
   **discovery jobs** (members never expanded under this scope), then
   **claim and spawn** the runnable members detached;
4. end the build when its plan is complete, or fail it when nothing is
   left to run; otherwise linger polling the flag, and on the way out
   re-read it once before and once after releasing the lease (the exit
   handshake that lets a worker skip spawning a tick while a scheduler is
   live).

Workers report their own lifecycle and wake the scheduler when they
finish; a periodic watchdog tick covers lost wake-ups. A worker that dies
without reporting simply stops: its claim lapses, a lapsed claim is
runnable, and the next claiming start takes it over — no probe, no report
window, no reaper.

A tick rebuilds every task object from its instance body
(``task_from_registry_data``), so it can resolve only classes whose modules
it imported; see ``stardag.build._task_modules``.
"""

from stardag.build._reactive._config import (
    DEFAULT_MAX_CONCURRENCY as DEFAULT_MAX_CONCURRENCY,
    TickConfig as TickConfig,
    TickSummary as TickSummary,
)
from stardag.build._reactive._frontier_actions import (
    act_on_frontier as act_on_frontier,
    spawn_cap as spawn_cap,
)
from stardag.build._reactive._lease import SchedulerLease as SchedulerLease
from stardag.build._reactive._rollover import (
    RollOver as RollOver,
    RollOverFailed as RollOverFailed,
    roll_over_aio as roll_over_aio,
)
from stardag.build._reactive._terminal import handle_terminal as handle_terminal
from stardag.build._reactive._tick import run_tick_aio as run_tick_aio

__all__ = [
    "RollOver",
    "RollOverFailed",
    "SchedulerLease",
    "TickConfig",
    "TickSummary",
    "act_on_frontier",
    "handle_terminal",
    "roll_over_aio",
    "run_tick_aio",
    "spawn_cap",
]
