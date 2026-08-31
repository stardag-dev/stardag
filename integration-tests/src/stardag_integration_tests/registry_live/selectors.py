"""Deployed-app callables for the registry-live DAG app.

In their own importable module rather than in the app module, because Modal
pickles a module-level function *by reference*: whatever container unpickles
the app's functions has to be able to import this. A callable defined in the
deploy entry point instead deploys cleanly and then kills every container
that tries to unpickle it.
"""

from __future__ import annotations

import stardag as sd

# A suggested key for the scenario that will test concurrency slots
# (nothing sets it yet). Named here rather than inline so that scenario and
# any `stardag concurrency-limits set` invocation cannot drift apart.
SLOW_LIMIT_KEY = "registry-live-slow"


def registry_live_limit_keys(task: sd.BaseTask) -> list[str]:
    """The key a task asked for, or none.

    Opt-in rather than by task name, because a limit key does more than
    feed a limiter. On every transition out of RUNNING the registry flags
    every build in the environment holding a PENDING task under the same
    key -- with no check that a limit is configured for it. So a key
    applied to every ``Slow`` task would couple the scenarios: they run
    concurrently in one environment, and one finishing would wake
    another's build, quietly supplying the "other explanation" that the
    wake-up scenarios assert cannot exist.

    Reading the key off the task instead means a scenario opts in exactly
    when the key is the thing under test, and the others are genuinely
    independent.
    """
    key = getattr(task, "limit_key", "")
    return [key] if key else []
