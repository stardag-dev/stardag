"""Tasks for the registry-live scenarios.

In their own module, and that is load-bearing rather than tidy: the app
declares ``task_modules=[...]`` covering exactly this module, so a scheduler
tick running in a container that has imported nothing else can still rebuild
these tasks from what the registry knows about them. If that stopped
working, a tick would have no way to reconstruct the plan and the scenarios
would fail at the first hand-off -- which is the point of exercising it.

The durations are not padding. Each one sizes a *window* some scenario needs
to exist, and the comments say which; shortening one to make a run faster is
how a scenario silently stops testing anything.
"""

from __future__ import annotations

import stardag as sd


@sd.task(name="Range")
def get_range(limit: int, salt: str) -> list[int]:
    """The chain's leaf, and the only place a run's identity enters it.

    ``salt`` does nothing to the result and everything to the *task id*.
    Task ids are derived from parameters, so a fresh salt makes this task
    and every task downstream of it new -- which is what stops a rerun
    finding the previous run's outputs already on the target root, calling
    every task complete, and passing having scheduled nothing.

    It is a parameter of the leaf rather than of each task so that the
    number of tasks in the plan is the same on every run, in a fresh
    environment or a reused one. Scenarios assert on how many tasks were
    spawned, and an assertion that depends on what a previous run happened
    to leave behind is not an assertion.
    """
    del salt
    return list(range(limit))


@sd.task(name="Square")
def square(values: sd.Depends[list[int]], offset: int) -> list[int]:
    return [(value + offset) ** 2 for value in values]


@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)


@sd.task(name="Slow")
def slow(values: sd.Depends[list[int]], seconds: int) -> list[int]:
    """Runs long enough to still be RUNNING when someone else asks for it.

    Two scenarios need that. The cross-build ones need a task genuinely
    in-flight rather than one that races to completion before the second
    build's first tick even fetches a frontier; and the wake-up scenario
    needs it to outlive the waiting build's tick, so that when it finishes
    there is provably no scheduler left anywhere to notice on its own.
    """
    import time

    time.sleep(seconds)
    return values
