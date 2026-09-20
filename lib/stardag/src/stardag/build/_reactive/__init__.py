"""Reactive (tick-based) build scheduling.

Instead of a resident orchestrator process that lives for the whole build,
reactive scheduling runs short-lived, idempotent **ticks**:

1. Acquire the build's scheduler lease (single-flight — a second concurrent
   tick exits immediately; the wake-up it was spawned for is covered by the
   holder's dirty-flag re-check).
2. Loop: clear the build's wake-up flag → fetch the frontier from the
   registry → act on it (spawn pending/suspended tasks detached, probe
   running refs, self-heal completions, handle terminal states) → linger
   briefly polling the wake-up flag → exit when quiet.
3. On the way out, re-read the flag once before releasing the lease and
   once after — the **exit handshake**, which is what makes it safe for a
   worker to skip spawning a tick while a scheduler is live. See
   :func:`_run_tick_body_aio`.

Acting on a frontier is **bounded-concurrent**, not serial: a wide layer
is thousands of independent registry round-trips and executor spawns, and
doing them one after another in a single container is the difference
between a tick that finishes and a tick that is killed by its function
timeout mid-fan-out. The bound (``TickConfig.max_concurrent_actions``) and
the per-tick spawn cap (``TickConfig.max_spawns_per_tick``) are what keep
"concurrent" from meaning "unbounded" — see :func:`_act_on_frontier` and
:func:`_spawn_cap`. Truncating at the cap is not a stall: the tick acted,
so it re-evaluates immediately on a fresh frontier rather than lingering.

Workers self-report their lifecycle (see the Modal ``Runner``) and wake the
scheduler when they finish, so no process needs to stay alive while
long-running tasks execute. A wake-up is "set the flag, then make sure
somebody looks at it", and the second half is skipped when the registry
answers the flag-set with ``scheduler_live`` — a tick already holds the
lease and will see it. On a build of short tasks that is the difference
between one working tick and one per completion, each paying a container
start to discover it has nothing to do. A periodic watchdog tick covers
lost wake-ups (worker died silently) and externally-triggered state
changes (e.g. build cancelled from the UI).

The tick is executor-agnostic: it only needs a :class:`TaskExecutorABC`
with detached support. Requirements and current limitations:

- A real registry (frontier computation is registry-backed; the reactive
  marker/owner live in the frontier's ``reactive_app_name``).
- Task objects are rehydrated from the :class:`BuildTaskStore` — written by
  the trigger (initial discovery) and by workers (dynamic deps) — or, when
  the pickle is absent, reconstructed from the registry's stored task data.
  The latter resolves only *registered* classes, i.e. classes whose
  defining module the tick process has imported; see
  ``stardag.build._task_modules`` for how an app declares those.
- The global concurrency lock and build-local ``ConcurrencyConfig`` limits
  are not applied by ticks (infra-level limits, e.g. Modal per-function
  ``concurrency_limit``, still apply). Registry-backed named limits *are*
  applied, via ``TickConfig.limit_key_selector``.
- On failure (FAIL_FAST) the build is failed, running executions are
  cancelled, and blocked descendants are marked SKIPPED (server-computed;
  older servers without the skip-blocked endpoint are tolerated).

**Retries.** A failure a tick *observes or causes* is retried, up to
``TickConfig.max_attempts`` attempts per task per build *round*. That is a
narrower promise than it sounds, and the narrowness is the point: the only
failures reaching the tick are the ones no execution backend can retry for
you — a spawn that failed before any container existed, and an execution
the backend killed or lost (OOM, preemption, a worker that vanished and
let its claim lapse). An exception *inside* the container never gets here
at all; the worker self-reports TASK_FAILED, which takes the task out of
the frontier, and covering that is what a backend's own function-level
retries (e.g. Modal's ``retries=``) are for. So this budget is spent on
infrastructure failures, not on deterministic ones — and a failure that
*is* deterministic from where the tick stands (a task whose object cannot
be rehydrated) is excluded explicitly.

A tick is short-lived and remembers nothing, so the count comes from the
server: ``FrontierTaskRef.attempt_count``, riding the frontier the tick
already fetches. Its window is the build's current **round** — starts
since the most recent BUILD_RESUMED, or since the build began. So the
budget is spent for the round, not for all time, and the escape is the one
operators already reach for: **re-trigger the build**, which emits
BUILD_RESUMED ahead of its discovery retries and starts every task at
zero again. A *bare* retry (the retry route, the UI's Retry button,
``stardag tasks retry``) emits no such event and does **not** reset the
budget — which is exactly the trap the second exhaustion message exists to
name, because from the operator's side the retry succeeds and then nothing
happens.

A build with nothing actionable and nothing running is genuinely stuck, and
fails rather than idling. That is a stronger statement than it used to be,
and it rests on the registry: dependency edges are scoped to a build's own
*structure scope* (the code and structure config that evaluated them — see
``docs/design/scope-keyed-dependency-structure.md``), the server re-closes
the plan over that scope before reporting a stalled frontier, so every
gating upstream is in the plan. A shared upstream another build is running
shows up in ``running`` and is probed like this build's own; one another
build cancelled — or one skipped because of an upstream that has since
completed — is listed as actionable and is reset and run by the pass that
sees it, within the same attempt budget as any retry. What is left when
nothing is actionable is a result, which ``fail_mode`` owns.

Every start this tick records carries a claim TTL derived from the
executor's own timeout (see :func:`claim_ttl_seconds`), so the expiry other
schedulers read is tied to the moment the execution is actually killed
rather than to a generic server-side default.

Each tick reports its :class:`TickSummary` to the registry on the way out
(``TickConfig.report_tick_summaries``), so the scheduler's own account of
what it did survives the container it ran in — a build driven by dozens of
short-lived ticks otherwise leaves its reasoning scattered across as many
logs. A tick that crashes is reported too, as ``outcome="error"``. Strictly
best-effort throughout: it never fails a tick, never changes an outcome,
never masks an exception, and tolerates a server without the endpoint.
"""

# One module per concern, split along the seams the docstring above already
# names (STA-20). Everything previously importable from
# ``stardag.build._reactive`` is re-exported here, with one deliberate
# exception: the MUTABLE module globals (the lease timing knobs, the
# tick-summary route latch, the successor-spawner warning latch) are not.
# Re-binding an alias of a mutable global does nothing to the module that
# reads it — a monkeypatch against this package would go green while
# patching nothing — so tests reach into the owning submodule instead.

from stardag.build._reactive._budgets import (
    _attempts_phrase as _attempts_phrase,
    _record_task_failure as _record_task_failure,
    _retry_allowed as _retry_allowed,
    _start_denied_by_budget as _start_denied_by_budget,
)
from stardag.build._reactive._discovery import (
    _DEFAULT_MAX_CONCURRENT_DISCOVER as _DEFAULT_MAX_CONCURRENT_DISCOVER,
    _RETRYABLE_STATUSES as _RETRYABLE_STATUSES,
    DiscoveryResult as DiscoveryResult,
    discover_and_register_aio as discover_and_register_aio,
)
from stardag.build._reactive._frontier_actions import (
    _CLAIM_TTL_GRACE_SECONDS as _CLAIM_TTL_GRACE_SECONDS,
    _DEFAULT_MAX_SPAWNS_PER_TICK as _DEFAULT_MAX_SPAWNS_PER_TICK,
    _INTERRUPTED_STATUS as _INTERRUPTED_STATUS,
    _MAX_CLAIM_TTL_SECONDS as _MAX_CLAIM_TTL_SECONDS,
    _MAX_SPAWN_CAP as _MAX_SPAWN_CAP,
    _MIN_CLAIM_TTL_SECONDS as _MIN_CLAIM_TTL_SECONDS,
    _MIN_SPAWN_CAP as _MIN_SPAWN_CAP,
    _RUNNING_STATUSES as _RUNNING_STATUSES,
    _SECONDS_PER_SPAWN as _SECONDS_PER_SPAWN,
    _SPAWN_BUDGET_FRACTION as _SPAWN_BUDGET_FRACTION,
    _TERMINAL_BUILD_STATUSES as _TERMINAL_BUILD_STATUSES,
    _act_on_frontier as _act_on_frontier,
    _claim_has_lapsed as _claim_has_lapsed,
    _derived_spawn_cap as _derived_spawn_cap,
    _load_task as _load_task,
    _spawn_cap as _spawn_cap,
    claim_ttl_seconds as claim_ttl_seconds,
)
from stardag.build._reactive._report_window import (
    _ReportWindow as _ReportWindow,
)
from stardag.build._reactive._terminal import (
    _handle_terminal as _handle_terminal,
    _skip_blocked as _skip_blocked,
)
from stardag.build._reactive._tick import (
    _DEFAULT_MAX_CONCURRENCY as _DEFAULT_MAX_CONCURRENCY,
    _MAX_ERROR_MESSAGE_CHARS as _MAX_ERROR_MESSAGE_CHARS,
    _NO_HANDOFF_OUTCOMES as _NO_HANDOFF_OUTCOMES,
    _TRUNCATION_MARKER as _TRUNCATION_MARKER,
    SchedulerLease as SchedulerLease,
    TickConfig as TickConfig,
    TickSummary as TickSummary,
    RollOver as RollOver,
    RollOverFailed as RollOverFailed,
    run_tick_aio as run_tick_aio,
)

__all__ = [
    "DiscoveryResult",
    "SchedulerLease",
    "TickConfig",
    "TickSummary",
    "RollOver",
    "RollOverFailed",
    "claim_ttl_seconds",
    "discover_and_register_aio",
    "run_tick_aio",
]
