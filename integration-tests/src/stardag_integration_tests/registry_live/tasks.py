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
def get_range(limit: int) -> list[int]:
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


@sd.task(name="SleepAndReport")
def sleep_and_report(seconds: int, salt: str) -> dict[str, str]:
    """Sleeps, then records which worker ran it.

    The sleep has to outlast the spawning tick's linger, or the tick is
    still around when the task finishes and the completion is observed
    rather than *reported* -- which is the self-heal path, not the reactive
    one, and it passes for the wrong reason.

    ``salt`` gives every run distinct task ids. Without it the second run
    finds the first run's outputs already on the target root, every task is
    complete before it starts, and the scenario passes having scheduled
    nothing at all.
    """
    import os
    import time

    time.sleep(seconds)
    return {
        "salt": salt,
        # Identifies the container, so a scenario can tell "the worker
        # reported this" from "a tick noticed it later".
        "modal_task_id": os.environ.get("MODAL_TASK_ID", ""),
    }
