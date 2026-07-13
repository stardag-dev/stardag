"""Worker and concurrency-limit selectors for the walkthrough app.

These callables are passed to ``StardagApp`` and end up **captured by the
serialized Modal functions** (the build function, the workers, and — for
reactive mode — the ``tick`` / ``tick_watchdog`` functions). Modal
cloudpickles those functions; a plain module-level function is pickled
*by reference* (its ``__module__`` + qualified name), so a fresh container
must be able to import the defining module to deserialize.

They therefore live in this small, dependency-light package module —
importable in every Modal container via
``add_local_python_source("stardag_examples")`` — rather than inside the
deploy script ``app.py``. When an app is deployed by file path
(``stardag modal deploy .../app.py``) Modal loads that file as a loose
top-level module named ``app``, which is *not* on the container's import
path; a selector defined there would deserialize to
``ModuleNotFoundError: No module named 'app'`` on the first cold container
(most visibly the scheduled watchdog tick). Defining them here keeps their
``__module__`` a stable, importable package path regardless of how
``app.py`` itself is deployed.

The same rule applies to any callable you hand to ``StardagApp`` (custom
build/run functions included): define it in a module that is part of the
Python source added to the image.
"""

import stardag as sd

# The named concurrency-limit key ProcessShard tasks run under. The cap is
# configured per environment in the registry (see configure_limits.py and
# the Concurrency Limits page in the UI).
SHARD_LIMIT_KEY = "walkthrough-shards"


# Both selectors match on task *name* (rather than importing the task
# classes) so they stay dependency-light — a cold container can import this
# module to resolve them without pulling in the task definitions.
def worker_selector(task: sd.BaseTask) -> str:
    if task.get_name() == "LongScan":
        return "long"
    return "default"


def limit_key_selector(task: sd.BaseTask) -> list[str]:
    if task.get_name() == "ProcessShard":
        return [SHARD_LIMIT_KEY]
    return []
