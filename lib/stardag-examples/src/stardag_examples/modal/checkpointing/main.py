"""Trigger the checkpointing example.

Watch the task go RUNNING → INTERRUPTED → RUNNING several times in the UI
before it completes. Each INTERRUPTED is the worker saying "the platform
took my container, I checkpointed" — not a failure, and it does not spend
the task's retry budget.
"""

from stardag_examples.modal.checkpointing.app import app
from stardag_examples.modal.checkpointing.task import TrainModel

if __name__ == "__main__":
    result = app.build_trigger(TrainModel(seed=0), reactive=True)
    print(f"build_id={result.build_id}")
