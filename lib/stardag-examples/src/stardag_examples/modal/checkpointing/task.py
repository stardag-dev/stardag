"""A task that expects to be killed and resumed until it converges.

The canonical "expected timeout" workload: work that needs more wall-clock
than the platform's maximum function timeout allows, so it checkpoints and
is *supposed* to be interrupted repeatedly. See
``docs/how-to/integrate-modal.md#preemption-and-timeouts``.
"""

import json
import time

import stardag as sd
from stardag.integration.modal import MODAL_INTERRUPTIONS

TOTAL_STEPS = 20
SECONDS_PER_STEP = 2.0


class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
    """"Trains" for ``TOTAL_STEPS``, surviving any number of interruptions.

    Deliberately slower than the worker's ``timeout`` (see ``app.py``), so
    a real run *will* be interrupted and resumed several times. That is the
    whole point of the example — a task like this is not misconfigured, and
    the platform ending it is not an error.
    """

    seed: int = 0

    def target(self) -> sd.DirectoryTarget:
        """A directory, so progress and the result can live side by side.

        ``TargetTask`` rather than ``Task`` because this task owns its
        target rather than letting a serializer pick one: ``Task.target()``
        is typed to return the serializer's ``LoadableSaveableFileSystemTarget``,
        so overriding it with a bare ``DirectoryTarget`` does not typecheck.
        ``TargetTask[DirectoryTarget]`` is the base for exactly this — a
        typed target you define, with ``complete()`` derived from it.

        ``DirectoryTarget.exists()`` is backed by a ``._DONE`` flag file
        written by ``mark_done()``, so writing a checkpoint inside it does
        **not** make the task look complete. That is the whole reason to
        use one here: a checkpoint is the opposite of a result — it says
        "not done, but do not start over".
        """
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self) -> None:
        checkpoint = self.target() / "checkpoint.json"

        state = {"step": 0, "loss": 10.0}
        if checkpoint.exists():
            with checkpoint.open("r") as handle:
                state = json.load(handle)
        print(f"TrainModel(seed={self.seed}): resuming from step {state['step']}")

        try:
            while state["step"] < TOTAL_STEPS:
                time.sleep(SECONDS_PER_STEP)
                state = {"step": state["step"] + 1, "loss": state["loss"] * 0.9}
                print(f"  step {state['step']}/{TOTAL_STEPS}, loss {state['loss']:.4f}")
        except MODAL_INTERRUPTIONS:
            # Exactly the two exceptions the platform ends an execution
            # with: KeyboardInterrupt when it reclaims the container, and
            # modal.exception.InputCancellation when the function timeout
            # fires. Catching this tuple rather than BaseException is
            # load-bearing — a NameError is a BaseException too, and
            # answering one with "resume me" would run a deterministic bug
            # until the interruption budget is gone.
            with checkpoint.open("w") as handle:
                json.dump(state, handle)
            print(f"  interrupted at step {state['step']}; checkpointed")
            # The load-bearing line. Raising this is the ONLY way a task
            # asks to be resumed — an interruption left to propagate is a
            # failure, on the reading that a task with no plan for being
            # interrupted either hung or has too small a timeout.
            raise sd.ResumableInterruption(
                f"checkpointed at step {state['step']}"
            ) from None

        with (self.target() / "result.json").open("w") as handle:
            json.dump({"steps": state["step"], "loss": state["loss"]}, handle)
        # Only now is the task complete.
        self.target().mark_done()
