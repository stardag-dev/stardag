"""Freeing a concurrency slot wakes the build that was queued on it.

A second wake-up mechanism, and it is genuinely a different one from the
cross-build case next door. There the news is about a *task*: a build holds
the task whose status changed, so the registry knows to flag it. Here the
two builds share no task at all. They want different work; they are queued
behind the same named limit, and the only thing connecting them is a
counter.

That makes the "who needs to hear about this?" question harder in a way
worth testing live: on a transition out of RUNNING the registry has to flag
not only the builds holding *that task* but the builds holding a PENDING
task under any limit key it was occupying. Nothing in the waiting build's
own state points at the task that freed the slot.

Without that flag the wait ends only at the watchdog, which this app does
not run at all. So if B finishes, the slot release reached across.

The limit is set here rather than assumed, and torn down afterwards. It is
environment-global -- one number shared by everything running against this
registry -- which is exactly why the app's key selector is **opt-in**: only
a task that asks for the key gets it, so no other scenario's work is
accounted against this limit or woken by its release.
"""

from __future__ import annotations

import sys
import uuid

import pytest
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    assert_remaining_work_outlasts_linger,
    assert_trail_complete,
    describe,
    require_complete_trail,
    tick_summaries,
    trail_may_be_truncated,
    wait_for_task_status,
    wait_for_terminal,
)
from stardag_integration_tests.registry_live.selectors import SLOW_LIMIT_KEY

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# A's task holds the single slot for this long. It has to outlive B's
# linger by a clear margin, or B is still resident when the slot frees and
# notices on its own poll -- which would pass while testing nothing.
A_SLOW_SECONDS = 60
B_LINGER_SECONDS = 15

# B's own task is short: once it has the slot there is nothing to prove by
# making it wait.
B_SLOW_SECONDS = 5

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


@pytest.fixture
def slot_limit():
    """One slot for the scenario's key, removed again afterwards.

    Torn down because a concurrency limit is environment-global and
    outlives the run that set it. Nothing else opts into this key today, so
    the leftover would be harmless today -- but it is a piece of registry
    configuration silently left behind on a stack meant to be kept and
    re-run against, and the next task to ask for the key would be
    serialized by a cap nobody set on purpose.
    """
    from stardag.registry import registry_provider

    registry = registry_provider.get()
    registry.concurrency_limit_set(SLOW_LIMIT_KEY, 1)
    try:
        yield
    finally:
        registry.concurrency_limit_delete(SLOW_LIMIT_KEY)


@pytest.mark.budget(120)
def test_releasing_a_slot_wakes_the_build_queued_on_it(
    slot_limit, deployment: Deployment
) -> None:
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )

    salt = uuid.uuid4().hex

    # Two *different* slow tasks. The builds share no task whatsoever --
    # only the limit key -- which is what distinguishes this from the
    # cross-build wake-up.
    a_slow = slow(
        values=get_range(limit=8, salt=salt),
        seconds=A_SLOW_SECONDS,
        limit_key=SLOW_LIMIT_KEY,
    )
    b_slow = slow(
        values=get_range(limit=9, salt=salt),
        seconds=B_SLOW_SECONDS,
        limit_key=SLOW_LIMIT_KEY,
    )

    build_a = app.build_trigger(
        get_sum(integers=a_slow),
        reactive=True,
        # A stays resident through its own work, so that the only build
        # depending on a wake-up is B.
        tick_kwargs={"linger_seconds": 180, "poll_interval_seconds": 3},
    ).build_id

    # Let A actually take the slot before B asks for one, so B is *denied*
    # rather than racing A for it. Waiting on the task's status is what
    # makes that certain -- a fixed sleep would only make it likely.
    wait_for_task_status(
        a_slow.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # The dormancy precondition, measured rather than assumed. The
    # shared task is already RUNNING, so its *total* duration says
    # nothing about whether this build will go dormant -- what
    # decides that is the work still to come when its tick starts.
    # Comparing the constant would let a slow bootstrap leave the
    # build resident through the completion while A_SLOW_SECONDS >
    # B_LINGER_SECONDS still looked reassuring.
    assert_remaining_work_outlasts_linger(
        deployment,
        a_slow.id,
        total_seconds=A_SLOW_SECONDS,
        linger_seconds=B_LINGER_SECONDS,
        what="A's remaining work against B's linger",
    )
    build_b = app.build_trigger(
        get_sum(integers=b_slow),
        reactive=True,
        tick_kwargs={
            "linger_seconds": B_LINGER_SECONDS,
            "poll_interval_seconds": 3,
        },
    ).build_id

    status_a = wait_for_terminal(build_a, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_a == "completed", describe(build_a)

    # B is the subject. If the slot release did not reach it, this hangs.
    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        "Build B never finished. Its task was denied the only slot of a "
        "named concurrency limit, the holder completed and freed it, and "
        "nothing told B. B shares no task with A, so the flag has to come "
        "from the limit key the finishing task held.\n"
        f"--- build A ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )

    summaries_b = tick_summaries(build_b)
    assert_trail_complete(build_b, summaries_b)

    # B really was denied the slot rather than merely being slow to start.
    # Without this the test would pass just as well with no limit set at
    # all, which is the failure mode worth guarding against here.
    # The one report-presence assertion kept in these four, and the
    # exception is reasoned rather than overlooked. A limit denial writes
    # **no durable event** -- it is counted in the tick summary and nowhere
    # else -- so there is no state to assert on instead; giving it one is
    # direction 3, deferred. Dropping it is worse than keeping it: without
    # it the scenario passes just as well with no limit in force, which is
    # the silently-weaker-test failure this file's timing rules exist to
    # prevent. What it risks is a preempted *early* tick, which softening
    # the terminal wait does not make more likely.
    require_complete_trail(build_b, what="the limit-denied count")
    denied = sum(s.get("limit_denied", 0) for s in summaries_b)
    assert denied >= 1, (
        "No tick of build B was ever denied a concurrency slot, so the "
        f"limit on {SLOW_LIMIT_KEY!r} was not in force and B simply ran. "
        "Nothing about slot-release wake-ups was exercised.\n" + describe(build_b)
    )

    # And it *was* dormant -- observed, not predicted. The measured
    # precondition bounds the work left when this build was *triggered*,
    # not when its tick actually started, so a slow bootstrap can still
    # eat the margin: necessary, but not sufficient on its own. A tick
    # that lingered out is the outcome itself.
    #
    # Through `require_complete_trail`, because this is a trail
    # observation and a preempted terminal tick must not redden the
    # scenario for it -- which is what STA-89 set out to stop. And `any`
    # rather than `summaries[0]`: when the first tick is preempted,
    # `summaries[0]` silently becomes the second one, while the question
    # the scenario means -- some tick lingered out and a later one did
    # the work -- does not depend on the order.
    require_complete_trail(build_b, what="whether any tick of build B lingered out")
    assert any(s.get("outcome") == "lingered_out" for s in summaries_b), (
        "No tick of build B ever lingered out, so B may have been "
        "resident when the slot freed and seen it on its own poll -- the "
        "wake-up path would then not have been exercised.\n" + describe(build_b)
    )

    # And B was dormant when the slot freed: A's task must outlast B's
    # linger, so B's tick cannot still have been resident to see it on its
    # own poll. From the constants, not from the trail -- see the helper.

    lingered = sum(1 for s in summaries_b if s.get("outcome") == "lingered_out")
    print(
        f"[harness] {len(summaries_b)} tick summary(ies) retained, "
        f"{lingered} reporting lingered_out"
        + (" (trail may be truncated)" if trail_may_be_truncated(build_b) else ""),
        file=sys.stderr,
    )

    # Nothing is asserted about *which* process spawned B's wake-up tick,
    # and the omission is deliberate rather than an oversight.
    #
    # An earlier version required A's tick trail to show a
    # `neighbour_ticks_spawned`, on the reasoning that A's side must have
    # done the waking. It failed on the first live run, and it was the
    # assertion that was wrong: A's whole build fitted inside a single
    # resident tick, and the drain that woke B was performed by A's
    # *worker* on the status write that freed the slot. A worker's drains
    # appear in no tick trail at all.
    #
    # Both routes are the mechanism working. Every notifier drains -- the
    # worker on its status write, the tick on its pass and again on its
    # exit -- precisely so that no single one of them is load-bearing, and
    # a test that named one turned that redundancy into a failure.
    #
    # What the assertions above already establish needs no such help: B was
    # denied the slot, B's own scheduler left, B later completed, and no
    # watchdog runs here. There is no route to that except the release
    # reaching across.
