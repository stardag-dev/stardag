"""A tick's configuration and the summary it reports."""

from __future__ import annotations

import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from stardag import BaseTask
from stardag.build._base import FailMode
from stardag.build._registration import DEFAULT_MAX_CONCURRENT_DISCOVER

if typing.TYPE_CHECKING:
    from stardag.build._wakeups import SpawnTick

# In-flight bound for a pass's per-task registry calls and spawns.
DEFAULT_MAX_CONCURRENCY = 50


# Keyword-only: the fields are grouped by meaning, and a positional caller
# would be silently re-bound by a field inserted beside its relatives.
@dataclass(kw_only=True)
class TickConfig:
    """Configuration for reactive scheduler ticks.

    ``linger_seconds`` spans the many-small-tasks ↔ few-long-tasks
    spectrum: while the DAG is churning the tick stays resident (each
    action resets the linger deadline); when only long-running work remains
    in flight it exits, and a worker's wake-up (or the watchdog) starts the
    next one.
    """

    linger_seconds: float = 120.0
    poll_interval_seconds: float = 3.0
    fail_mode: FailMode = FailMode.FAIL_FAST
    # How many times a pass tries to *spawn* a claimed execution before it
    # records the failure. A spawn that fails before any container exists
    # (the backend refused it for a moment) is the one failure a tick
    # causes itself and can retry on the spot, statelessly; everything
    # else is recovered by the registry: a worker that dies lets its claim
    # lapse, and a lapsed claim is taken over by the next claiming start.
    max_attempts: int = 2
    # How many interruptions a task may have within this build before the
    # tick stops restarting it. An INTERRUPTED member is actionable (the
    # platform ended an execution that asked to be resumed), so without a
    # cap a task that times out on every run would be restarted forever.
    # Counted by the registry from the execution ledger over all of the
    # build's plans (the frontier's ``interruptions``, design.md D9). At
    # the cap the tick does not spawn the member: it records a TASK_FAILED
    # naming the count, and the build's fail mode applies. Set generously:
    # 20 resumes of a long training run is a plausible afternoon, 20
    # identical timeouts of a hung task a clear signal and a bounded bill.
    max_interruptions: int = 20
    # In-flight bound for the pass's per-task work (claims, spawns, ref
    # records, discovery jobs' registrations).
    max_concurrent_actions: int = DEFAULT_MAX_CONCURRENCY
    # Completion checks in flight while a discovery job walks.
    max_concurrent_discover: int = DEFAULT_MAX_CONCURRENT_DISCOVER
    # Hard cap on the spawns of ONE pass. None derives it from a wall clock
    # (see ``stardag.build._reactive._frontier_actions.spawn_cap``);
    # truncating is a throttle, not a stall: the pass acted, so the tick
    # re-reads the frontier immediately.
    max_spawns_per_tick: int | None = None
    # The wall-clock limit of the container running THIS tick, when the
    # caller knows it (the Modal integration knows its ``tick`` function's
    # ``timeout``). Deployment infrastructure: not a per-build tick kwarg.
    tick_timeout_seconds: float | None = None
    # Maps a task to the registry concurrency-limit keys it runs under; the
    # keys travel on the claiming start, computed from the instance body
    # the tick is about to run. A full key denies the claim and the task
    # stays in the frontier until a slot frees.
    limit_key_selector: Callable[[BaseTask], Sequence[str]] | None = None
    # How to spawn a tick for a build on a deployed app: the exit hand-off
    # and the cross-build drain. ``None`` disables both — correct only for
    # a caller whose wake-ups always spawn a tick.
    spawn_tick: "SpawnTick | None" = None
    # Report each tick's summary to the registry (best-effort).
    report_tick_summaries: bool = True


@dataclass(kw_only=True)
class TickSummary:
    """Outcome of one scheduler tick. Flat and JSON-friendly: reported to
    the registry with ``dataclasses.asdict``."""

    # "not_reactive" | "lease_held" | "lease_lost" | "terminal" |
    # "lingered_out" | "superseded" | "rollover_failed" | "error"
    outcome: str
    terminal_status: str | None = None
    # Set for "error" and "rollover_failed": what ended the tick (bounded).
    error_type: str | None = None
    error_message: str | None = None
    iterations: int = 0
    # Executions this tick claimed and spawned.
    spawned: int = 0
    # Claimed executions whose spawn failed max_attempts times, recorded as
    # the execution's failure.
    spawn_failed: int = 0
    # Interrupted members this tick failed instead of restarting, because
    # their interruptions reached max_interruptions.
    interruptions_exhausted: int = 0
    # Discovery jobs this tick completed (an unexpanded member expanded and
    # registered with its closure).
    discovered: int = 0
    # Members this tick excluded from the plan (discovery_failed): a class
    # this code cannot import, a requires() that raised, a body that does
    # not rehydrate.
    excluded: int = 0
    # Claims refused because another execution holds the task, it completed
    # meanwhile, or an upstream is not COMPLETED yet.
    claim_denied: int = 0
    # Claims refused because a concurrency limit on their keys is full.
    limit_denied: int = 0
    # Members marked SKIPPED when the build failed.
    skipped: int = 0
    # 1 when this tick re-planned the build under its own deployment.
    rolled_over: int = 0
    # Containers this tick spawned and then stopped because the registry
    # refused their start (the claim moved on during the spawn).
    cancelled_refs: int = 0
    # Exit handshake: the linger deadline expired with the wake-up flag set,
    # so the tick kept the lease and re-acted.
    linger_extended: int = 0
    # Successor ticks spawned for a flag set while the lease was released.
    successor_spawned: int = 0
    # Ticks spawned for other builds the registry flagged.
    neighbour_ticks_spawned: int = 0
