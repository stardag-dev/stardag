"""StardagApp definition for the Modal integration walkthrough.

Compared to the minimal ``modal/basic`` example, this app configures the
full (new) feature set:

- ``builder_settings`` with ``retries``: Modal re-runs the build function
  after infrastructure failures, and — combined with ``build_trigger`` —
  each retry *resumes* the same registry build instead of starting a new
  one.
- Two workers: ``default`` for ordinary tasks and ``long`` with a higher
  timeout for the long-running task; ``worker_selector`` routes by task
  name. Configured on the app (not per trigger) so reactive scheduler
  ticks apply the same routing.
- ``watchdog_period_minutes``: a scheduled function that periodically
  re-ticks running reactive builds — the safety net for lost wake-ups,
  builds cancelled from the UI, and stale concurrency-limit slots.
  Strongly recommended whenever reactive mode or named limits are used.
- ``limit_key_selector``: tags every ``ProcessShard`` task with the
  ``SHARD_LIMIT_KEY`` named concurrency limit. The cap itself lives in
  the registry, per environment — see ``configure_limits.py``. Like the
  worker selector this is deployed-app configuration, applied
  consistently by every scheduler tick.

Deploy with the active stardag profile (see the README):

    stardag modal deploy src/stardag_examples/modal/walkthrough/app.py

then trigger builds with ``main.py``.
"""

import sys

import modal
import stardag as sd
import stardag.integration.modal as sd_modal

# The named concurrency-limit key ProcessShard tasks run under. The cap is
# configured per environment in the registry (see configure_limits.py and
# the Concurrency Limits page in the UI).
SHARD_LIMIT_KEY = "walkthrough-shards"

# Must match local Python version for Modal serialization compatibility
python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

# Define the Modal image
image = (
    modal.Image.debian_slim(python_version=python_version)
    .uv_sync()
    .add_local_python_source("stardag_examples")
)

# Optionally use this instead to use local source for stardag itself.
# image = sd_modal.with_stardag_on_image(
#     modal.Image.debian_slim(python_version=python_version).pip_install(
#         # helper to pull in all dependencies of current package (stardag-examples)
#         *sd_modal.get_package_deps(__file__),
#     )
# ).add_local_python_source("stardag_examples")


# Both selectors match on task *name* (rather than importing the task
# classes) so this module stays a pure deployment definition.
def worker_selector(task: sd.BaseTask) -> str:
    if task.get_name() == "LongScan":
        return "long"
    return "default"


def limit_key_selector(task: sd.BaseTask) -> list[str]:
    if task.get_name() == "ProcessShard":
        return [SHARD_LIMIT_KEY]
    return []


app = sd_modal.StardagApp(
    "stardag_examples-walkthrough",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # required for communication with registry
            modal.Secret.from_name("stardag-api-key"),
        ],
        # Let Modal restart the build function after infrastructure
        # failures; with build_trigger each restart resumes the same build.
        retries=2,
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image, cpu=1),
        # Long-running tasks get their own worker with a generous timeout.
        "long": sd_modal.FunctionSettings(image=image, cpu=1, timeout=1800),
    },
    worker_selector=worker_selector,
    # Reactive-mode safety net: periodically re-check running builds
    # (lost wake-ups, UI cancellations, stale limit slots).
    watchdog_period_minutes=5,
    limit_key_selector=limit_key_selector,
)
