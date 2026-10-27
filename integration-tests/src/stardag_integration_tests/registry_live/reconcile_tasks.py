"""The reconciliation scenarios' tasks that are not in ``tasks``.

In their own module because ``tasks`` sits at the module size ceiling. A
module of the reconcile app's ``task_modules`` (see ``_scenario_app``), so a
tick can rebuild these from registry data alone, as it can ``tasks``.
"""

from __future__ import annotations

import os

import stardag as sd

from ._gates import hold
from .selectors import RETRYING_WORKER

# The artifacts ``FailsOnceUnreported`` records its attempts in (see its
# docstring).
FAILED_ATTEMPT_ARTIFACT_NAME = "registry-live-failed-attempt"
RETRY_ATTEMPT_ARTIFACT_NAME = "registry-live-retry-attempt"


class FailsOnceUnreported(sd.Task[list[int]]):
    """A task whose first attempt raises unreported, and whose Modal retry
    then runs long, under the same call (STA-146).

    Routed to the reconcile app's ``retrying`` worker, which has Modal
    ``retries`` set, so the exception out of the first attempt is retried by
    Modal on the same function call -- the same executor ref, the same
    execution. The reconcile app's ``faults`` withhold this task's failure
    report, as a rate limit outlasting its retries would: the ledger keeps
    the execution running, and only a probe of the call can say anything
    about it. That is the state the scenario is about.

    * **The first attempt** records ``FAILED_ATTEMPT_ARTIFACT_NAME`` (its
      execution) and raises.
    * **The retry** -- any attempt that finds that record for its own
      execution -- records ``RETRY_ATTEMPT_ARTIFACT_NAME``, then holds on
      ``gate`` (``seconds`` at most, see ``_gates``) while the scenario
      probes the call, and writes its output.

    "First" is read off the registry for the reason ``DiesOnce`` gives: a
    retry may land in a fresh container, which has no state of its own.
    """

    salt: str
    seconds: int = 240
    gate: str = ""
    worker: str = RETRYING_WORKER

    def run(self):
        import sys
        from uuid import UUID

        from stardag.artifact import JSONArtifact
        from stardag.registry import registry_provider

        registry = registry_provider.get()
        execution_id = UUID(os.environ["STARDAG_EXECUTION_ID"])
        plan_id = UUID(os.environ["STARDAG_PLAN_ID"])
        task_id = str(self.id)
        failed_before = any(
            a.name == FAILED_ATTEMPT_ARTIFACT_NAME
            and (a.body or {}).get("execution_id") == str(execution_id)
            for a in registry.task_list_artifacts(task_id)
        )
        name = (
            RETRY_ATTEMPT_ARTIFACT_NAME
            if failed_before
            else FAILED_ATTEMPT_ARTIFACT_NAME
        )
        registry.task_upload_artifacts(
            plan_id,
            task_id,
            [JSONArtifact(name=name, body={"execution_id": str(execution_id)})],
            execution_id=execution_id,
        )
        if not failed_before:
            print(
                f"[registry-live] FailsOnceUnreported {task_id}: attempt 1 of "
                f"execution {execution_id} raises",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("FailsOnceUnreported: the first attempt fails")
        print(
            f"[registry-live] FailsOnceUnreported {task_id}: the retry of "
            f"execution {execution_id} runs",
            file=sys.stderr,
            flush=True,
        )
        hold(self.gate, self.seconds, what="FailsOnceUnreported")
        self._save([0])
