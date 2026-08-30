"""Modal app for the checkpoint-and-resume example.

Deliberately configured so the interruption path actually runs: the worker's
``timeout`` is far shorter than the task needs, so every attempt is cut off
and resumed until the work is done.

Usage:
    stardag modal deploy app.py
    python main.py
"""

import sys

import modal
import stardag.integration.modal as sd_modal

# Must match local Python version for Modal serialization compatibility.
python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

image = (
    modal.Image.debian_slim(python_version=python_version)
    .uv_sync()
    .add_local_python_source("stardag_examples")
)

app = sd_modal.StardagApp(
    "stardag_examples-checkpointing",
    task_modules=["stardag_examples.modal.checkpointing.task"],
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={
        # 15s against a task that wants ~40s of work: three or four
        # interruptions before it converges. `retries=0` on purpose — this
        # example is about the *scheduler* resuming the task, so letting
        # Modal retry the input as well would muddy what you see in the UI.
        #
        # A real deployment would size `timeout` to the platform maximum and
        # let the task take as many resumes as it takes; small numbers here
        # only make the behaviour observable in under a minute.
        "default": sd_modal.FunctionSettings(image=image, timeout=15, retries=0),
    },
    # Nothing to configure for the interruption behaviour: the task asks
    # to be resumed by raising sd.ResumableInterruption, and the scheduler
    # obliges up to TickConfig.max_interruptions (default 20).
    # Reactive scheduling is what resumes the task. The watchdog stays off:
    # the interrupted worker reports before it dies and wakes the scheduler
    # directly, so nothing here depends on a timer.
)
