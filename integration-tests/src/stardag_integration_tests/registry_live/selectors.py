"""Deployed-app callables for the registry-live DAG app.

In their own importable module rather than in the app module, because Modal
pickles a module-level function *by reference*: whatever container unpickles
the app's functions has to be able to import this. A callable defined in the
deploy entry point instead deploys cleanly and then kills every container
that tries to unpickle it.
"""

from __future__ import annotations

import stardag as sd

SLOW_LIMIT_KEY = "registry-live-slow"


def registry_live_limit_keys(task: sd.BaseTask) -> list[str]:
    """Every ``Slow`` task occupies one ``registry-live-slow`` slot.

    Used by the scenario that checks a released concurrency slot wakes the
    build waiting on it; inert for the others, which run no ``Slow`` tasks.
    """
    return [SLOW_LIMIT_KEY] if task.get_name() == "Slow" else []
