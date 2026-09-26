"""Gates: a task body holds a state open until the scenario releases it.

Several scenarios need a task to *still be running* while they do something
else -- redeploy the app, trigger a second build, stop some calls. They used
to size that as a sleep in the task body: long enough for a deploy, a cold
start and a margin. A guess, and it failed in both directions. Too short and
the scenario flaked ("raise PRE_YIELD_SECONDS"); long enough never to flake,
and every run paid the whole window, twice where a re-plan re-ran the body.

A gate replaces the guess with a state. The task body *holds* until the
scenario releases the gate, and the scenario releases it once the state it
needed exists -- after the redeploy, after the second build's yield. So the
window is as long as the scenario actually needed it to be, and a body that
runs again after the release (a restart under new code, a re-plan) finds the
gate open and does not wait at all. "Wait on a state, never on a clock",
applied to the task bodies as well as to the test.

**The sleep it replaced stays, as the upper bound.** A hold ends on the
release *or* at the bound, whichever comes first, and the bound is exactly
the duration the scenario used to sleep. A release that is lost -- a Modal
control-plane fault on either side, a scenario that failed before releasing
-- degrades the run to today's behaviour rather than hanging it, and the
container log says which of the two ended the hold. It can never end
*early*: an unreadable gate is a closed gate, never an open one, because an
early release is the one failure that would silently stop a scenario testing
anything.

**Backed by a Modal Dict in the run's own environment.** The task bodies run
in Modal containers of that environment and the scenario runs on the runner
(or a laptop), so the one store both reach with credentials they already
hold is Modal's. The Dict goes with ``modal environment delete`` like the
rest of the stack. The test side names the environment explicitly, for the
reason ``_deployed.deployed_function`` gives; the container side resolves
its own environment, which is by construction the run's.

**A gate's key is a task field**, so it is part of the task's salted
identity: two scenarios, or two runs of one, cannot share a gate, and a gated
task is never the same task as an ungated one. An empty key means ungated:
the body sleeps its bound exactly as it always did.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

# One Dict per Modal environment; gates are keys in it.
GATES_DICT_NAME = "registry-live-gates"

# How often a held task re-reads its gate. Short relative to every window a
# gate replaces (the shortest is tens of seconds), and cheap: a handful of
# gated tasks at a time, one small read each.
POLL_INTERVAL_SECONDS = 1.0

# Prefix of the records a hold writes back when it ends, so the scenario can
# report how each hold ended (see ``GateSet.report``).
_HELD_PREFIX = "held:"

HoldOutcome = Literal["released", "bound", "ungated"]


def _log(message: str) -> None:
    print(f"[registry-live] gate {message}", file=sys.stderr, flush=True)


def _container_dict():
    """The gates Dict, from inside a scenario container.

    Modal sets ``MODAL_ENVIRONMENT`` in every container to the environment
    its app runs in -- the run's -- and a container's client resolves
    objects there by default too; naming it is only explicitness.
    """
    import modal

    return modal.Dict.from_name(
        GATES_DICT_NAME,
        environment_name=os.environ.get("MODAL_ENVIRONMENT") or None,
        create_if_missing=True,
    )


def hold(
    gate: str,
    bound_seconds: float,
    *,
    what: str = "",
    read: Callable[[str], bool] | None = None,
    record: Callable[[str, dict], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> HoldOutcome:
    """Hold until ``gate`` is released, or for ``bound_seconds`` at most.

    Called from a task body. Returns how the hold ended, which the body may
    ignore; the container log records it either way.

    ``read`` answers "is this gate released?", and **any exception it raises
    is read as "not yet"**: a fault reading the gate must never open it.
    ``record`` writes the hold's outcome back, best-effort. Both default to
    the run's Modal Dict; tests substitute their own, and the clock and
    sleep with them.
    """
    if not gate:
        sleep(bound_seconds)
        return "ungated"

    if read is None or record is None:
        store = _container_dict()

        def _read(key: str) -> bool:
            return store.get(key) is not None

        def _record(key: str, value: dict) -> None:
            store.put(key, value)

        read = read or _read
        record = record or _record

    started = clock()
    deadline = started + bound_seconds
    failed_reads = 0
    last_error: Exception | None = None
    outcome: HoldOutcome = "bound"
    while True:
        try:
            if read(gate):
                outcome = "released"
                break
        except Exception as error:  # a closed gate, never an open one
            failed_reads += 1
            last_error = error
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(poll_interval, remaining))

    held = clock() - started
    label = f"{gate} ({what})" if what else gate
    faults = (
        f"; {failed_reads} failed reads, last {last_error!r}" if failed_reads else ""
    )
    if outcome == "released":
        _log(f"{label}: released after {held:.1f}s{faults}")
    else:
        _log(
            f"{label}: NOT released within its {bound_seconds:.0f}s bound; "
            f"continuing as the ungated sleep would have{faults}"
        )
    try:
        record(
            f"{_HELD_PREFIX}{gate}:{os.getpid()}:{time.time():.3f}",
            {
                "gate": gate,
                "what": what,
                "outcome": outcome,
                "held_seconds": round(held, 1),
                "bound_seconds": bound_seconds,
                "failed_reads": failed_reads,
            },
        )
    except Exception as error:  # the record is a diagnostic, not the gate
        _log(f"{label}: could not record the hold's outcome: {error!r}")
    return outcome


@dataclass
class Gate:
    """The scenario's end of one gate: a key to hand a task, and a release."""

    key: str
    modal_environment: str
    released_at: float | None = None

    def release(self, *, attempts: int = 5, backoff_seconds: float = 2.0) -> bool:
        """Open the gate. Idempotent; retried, because Modal's control plane
        is the same edge the tier's transport faults come from.

        Returns False, loudly, when every attempt failed. The scenario then
        carries on: the held task reaches its bound, which is the old
        timing, so a lost release costs time and never correctness.
        """
        store = _test_dict(self.modal_environment)
        for attempt in range(1, attempts + 1):
            try:
                store.put(self.key, time.time())
            except Exception as error:
                _log(f"{self.key}: release attempt {attempt} failed: {error!r}")
                if attempt < attempts:
                    time.sleep(backoff_seconds * attempt)
                continue
            self.released_at = time.monotonic()
            _log(f"{self.key}: released by the scenario")
            return True
        _log(
            f"{self.key}: could NOT be released; the held task will run to its "
            "bound (the old timing)"
        )
        return False


def _test_dict(modal_environment: str):
    """The gates Dict, from the scenario's side, in the named environment."""
    import modal

    return modal.Dict.from_name(
        GATES_DICT_NAME,
        environment_name=modal_environment,
        create_if_missing=True,
    )


@dataclass
class GateSet:
    """The gates one scenario opened, so its teardown can say how each held.

    Handed out by the ``gates`` fixture. ``report`` reads back the records
    the holds wrote and prints one line per hold, flagging any that ran to
    its bound: a scenario that got slow because a release was lost looks
    exactly like a scenario that got slow for a real reason, unless
    something says which.
    """

    modal_environment: str
    gates: list[Gate] = field(default_factory=list)

    def new(self, name: str, *, salt: str) -> Gate:
        """A gate for this scenario, keyed by its salt."""
        gate = Gate(key=f"{name}-{salt}", modal_environment=self.modal_environment)
        self.gates.append(gate)
        return gate

    def holds(self, *, remove: bool = False) -> list[dict]:
        """Every hold recorded against this scenario's gates.

        ``remove`` also deletes those records and the gates' own keys, so
        an environment reused across runs (a developer's stack) does not
        accumulate them.
        """
        if not self.gates:
            return []
        keys = {gate.key for gate in self.gates}
        store = _test_dict(self.modal_environment)
        found = [
            (key, value)
            for key, value in store.items()
            if isinstance(key, str)
            and key.startswith(_HELD_PREFIX)
            and isinstance(value, dict)
            and value.get("gate") in keys
        ]
        if remove:
            for key in [*(k for k, _ in found), *keys]:
                try:
                    store.pop(key, None)
                except Exception as error:  # housekeeping only
                    _log(f"gates: could not remove {key}: {error!r}")
        return [value for _, value in found]

    def report(self) -> list[str]:
        """One line per recorded hold; the ones that hit their bound flagged."""
        try:
            holds = self.holds(remove=True)
        except Exception as error:  # diagnostics only
            return [f"gates: could not read the hold records: {error!r}"]
        lines = []
        for held in sorted(holds, key=lambda h: (h["gate"], h["held_seconds"])):
            flag = "" if held["outcome"] == "released" else "  <-- ran to its bound"
            lines.append(
                f"gate {held['gate']} ({held.get('what') or '-'}): "
                f"{held['outcome']} after {held['held_seconds']}s of "
                f"{held['bound_seconds']:.0f}s{flag}"
            )
        return lines


def wait_for_the_ticks_to_exit(
    deployment,
    build_id,
    *,
    task_id,
    bound_seconds: float,
    poll_interval: float = 3.0,
    settle_seconds: float = 8.0,
) -> bool:
    """Wait until no tick of ``build_id`` survives from before ``task_id``'s
    last event -- the precondition for releasing a gate across a redeploy.

    **Why a gated scenario needs this and the sleeps did not.** A tick that
    is still lingering when the held task finishes drives the build on
    under *its own* deployment. In a rollover scenario that tick is the old
    code's, so the build would complete on the old plan and never roll over
    -- or, in S7, drive the late yield's children on the plan the scenario
    says nobody drives. The old windows (110-240 s) were far longer than a
    linger (30-60 s), so the old tick had always exited; a gate released
    the moment the redeploy lands can beat it. So the release waits for the
    old tick too, as a state rather than as another guess.

    **The state: a tick summary written after the task's last event.**
    Nothing wakes a build while its one running task is held, so the only
    ticks that can be live are the one that started the task, or the one
    its RUNNING transition spawned when none was live -- and either exits
    after that transition and reports on the way out. A summary newer than
    the task's latest event is therefore the last live tick's.

    Bounded, because a tick killed mid-report writes no summary (STA-87):
    at ``bound_seconds`` it gives up, says so, and returns False, and the
    caller releases anyway. Size the bound as the build's linger plus a
    margin; past that, a tick that has not reported is not lingering.
    """
    from datetime import datetime, timezone

    from stardag.registry import registry_provider

    from ._events import task_events

    def _aware(value) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    registry = registry_provider.get()
    deadline = time.monotonic() + bound_seconds
    while True:
        try:
            stamps = [
                _aware(e.get("created_at")) for e in task_events(deployment, task_id)
            ]
            last_event = max((s for s in stamps if s is not None), default=None)
            reported = [
                _aware(r.created_at)
                for r in registry.build_list_tick_summaries(build_id, limit=50)
            ]
            if last_event is not None and any(
                r is not None and r > last_event for r in reported
            ):
                # The tick has exited; its container outlives it by the
                # app's scaledown window, and a spawn landing in it would
                # run the old code. The rollover apps keep that window at
                # ``ROLLOVER_SCALEDOWN_SECONDS``; wait it out, with margin.
                time.sleep(settle_seconds)
                return True
        except Exception as error:  # a read fault is "not yet", like a gate
            _log(f"tick-exit check for build {build_id} failed: {error!r}")
        if time.monotonic() >= deadline:
            _log(
                f"no tick of build {build_id} reported after task {task_id}'s "
                f"last event within {bound_seconds:.0f}s; releasing anyway"
            )
            return False
        time.sleep(poll_interval)
