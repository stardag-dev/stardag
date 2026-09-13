"""An abandoned fan-out is not re-run by the next build that wants its parent.

Dynamic dependency edges are written ``ON CONFLICT DO NOTHING``, so a task's
set only ever grew across attempts. That is invisible while the previous
generation completed -- completed upstreams do not gate -- and expensive the
moment one is abandoned part-way: the children gate *their own parent*
forever. The parent cannot be scheduled until they complete, so the next
build resets and runs a whole generation of work the task is no longer going
to ask for, and only then does the parent re-yield the generation it
actually needs.

The scenario is two builds of one root, with the fan-out changed between
them -- the ordinary reason a fan-out changes, an internal partitioning the
task's output does not depend on. The parent's id is unchanged, correctly:
a task id promises a world state, not a provenance.

Which is also why the width comes from a Modal Dict rather than a parameter.
A hashed parameter would make the two attempts two different tasks and the
scenario imaginary; a hash-excluded one is frozen at first registration by
the registry, and this app is ``require_pickle_free``, so the second build
would silently re-run the first generation's width. See
``GenerationalParent``.

Against the code this fixes, build 2 resets the parent, is still gated on
the abandoned children, resets those too and runs them -- seven spawns
instead of five.

The assertion is on build 2's spawn count rather than on the abandoned
children's statuses, and that is not a convenience. A cascade releases the
claims a build *holds*, so which of them end up CANCELLED and which are
left PENDING depends on how many had started when the cancel landed, and
one can even be RUNNING afterwards from a spawn build 1's tick had already
decided on. All of that is build 1's business and says nothing about
whether build 2 re-ran the generation.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    describe,
    task_status,
    tick_summaries,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# The two generations, as ``slow`` durations. Different durations mean
# genuinely different task ids, which is what two partitionings of one
# dataset are. Short: the first generation must not finish before the build
# is cancelled, and the second is real work the scenario waits out.
FIRST_GENERATION = [45, 46]
SECOND_GENERATION = [20, 21]

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900


def test_an_abandoned_generation_is_not_re_run() -> None:
    import modal

    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        FANOUT_DICT_NAME,
        GenerationalParent,
        get_range,
        get_sum,
        slow,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=1, salt=salt)
    parent = GenerationalParent(salt=salt)
    root = get_sum(integers=parent)

    def children(generation: list[int]) -> list:
        return [slow(values=leaf, seconds=seconds) for seconds in generation]

    fanout = modal.Dict.from_name(FANOUT_DICT_NAME, create_if_missing=True)
    fanout[salt] = FIRST_GENERATION
    try:
        build_one = app.build_trigger(
            root,
            reactive=True,
            tick_kwargs={"linger_seconds": 120, "poll_interval_seconds": 3},
        ).build_id

        # Wait for the yield to have happened: SUSPENDED is the parent
        # saying "I have registered my children and returned". Only then is
        # there a generation to abandon.
        wait_for_task_status(
            parent.id,
            expected="suspended",
            build_id=build_one,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        first = children(FIRST_GENERATION)

        # Abandon it, claims and all. The cascade reaches the parent and the
        # children alike: build one owns every one of them.
        from stardag.registry import registry_provider

        registry_provider.get().build_cancel(build_one, cascade=True)
        assert task_status(parent.id) == "cancelled", describe(build_one)

        # The code moves on: same parent, different internal partitioning.
        fanout[salt] = SECOND_GENERATION
        second = children(SECOND_GENERATION)

        build_two = app.build_trigger(
            root,
            reactive=True,
            tick_kwargs={"linger_seconds": 240, "poll_interval_seconds": 3},
        ).build_id

        status = wait_for_terminal(build_two, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", describe(build_two)

        # The point of the run, measured on build two's own trail rather
        # than on the children's statuses.
        #
        # Their statuses cannot carry it. A cascade releases the claims a
        # build *holds*, so which of the abandoned children are CANCELLED
        # and which are left PENDING depends on how many had started when
        # the cancel landed — and one can even be RUNNING afterwards, put
        # there by a spawn the first build's tick had already decided on
        # when the cascade committed. All of that is the first build's
        # business; none of it says whether the second build re-ran the
        # generation, which is the claim under test.
        #
        # What does say it is how much the second build spawned. Five is its
        # own work exactly: the parent twice (it suspends on its children
        # and is re-spawned to finish), the two children of the *current*
        # generation, and the root. The leaf is already complete from the
        # first build. Against the code this fixes the count was seven —
        # the same five plus the two abandoned children, reset to un-gate a
        # parent that was never going to ask for them again.
        summaries = tick_summaries(build_two)
        spawned = sum(s.get("spawned", 0) for s in summaries)
        stale = {str(task.id): task_status(task.id) for task in first}
        assert spawned == 5, (
            f"Build two spawned {spawned} tasks, not the 5 of its own work "
            "(the parent twice, its two current children, and the root). "
            "Seven means it re-ran the abandoned generation.\n"
            f"abandoned children: {stale}\n" + describe(build_two)
        )

        # ...and the generation it did ask for ran.
        fresh = {str(task.id): task_status(task.id) for task in second}
        assert set(fresh.values()) == {"completed"}, (
            f"The current generation did not complete: {fresh}\n" + describe(build_two)
        )
    finally:
        try:
            fanout.pop(salt)
        except KeyError:  # pragma: no cover - cleanup only
            pass
