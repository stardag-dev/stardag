"""The reconcile app's faults: a tick that dies between claim and spawn, a
worker whose end report never arrives, and a failed attempt whose failure
report never arrives.

Installed by ``install_reconcile_faults``, the ``container_setup`` of
``reconcile_app`` and of no other app. Container setup is the one hook that
reaches the *tick* as well as the workers (a tick container has no
``Builder`` and no ``Runner`` in it), and both faults have to sit inside
the SDK's own sequence -- after the claim, before the spawn; after the run,
in place of the report -- where no public seam exists. So each wraps one
SDK method at class level, and acts only for its own task class from
``tasks``; every other task in the container goes through untouched.

**Each fault records itself, durably, before it strikes**, as an artifact
on its task naming the execution it struck. That is the scenario's
evidence that the fault happened, to which execution, and -- for the tick
fault -- the only state that says "already died once": a container has
none that survives it.

In their own importable module rather than in ``reconcile_app``, for the
reason ``selectors`` is: Modal pickles a module-level callable by
reference, and every container that unpickles the app's functions has to
be able to import it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing
from uuid import UUID

from .tasks import (
    TICK_DEATH_ARTIFACT_NAME,
    UNREPORTED_END_ARTIFACT_NAME,
    EndGoesUnreported,
    FailsOnceUnreported,
    SpawnDiesWithTheTick,
)

_INSTALLED = "_registry_live_reconcile_fault"


def _say(message: str) -> None:
    print(f"[registry-live] {message}", file=sys.stderr, flush=True)


def _die_unless_died_before(task: typing.Any, execution_id: UUID) -> None:
    """The tick fault's body, run where the tick is about to spawn ``task``
    under a claim it has just taken as ``execution_id``.

    Exits the container unless this task's tick has died once already.
    Anything that keeps it from recording the death first -- a registry
    read or write failing -- lets the spawn through rather than dying
    unrecorded: a death the scenario cannot attribute is worse than no
    death, which it reports as such.
    """
    from stardag.artifact import JSONArtifact
    from stardag.build import get_current_build_context
    from stardag.registry import registry_provider

    try:
        registry = registry_provider.get()
        task_id = str(task.id)
        if any(
            a.name == TICK_DEATH_ARTIFACT_NAME
            for a in registry.task_list_artifacts(task_id)
        ):
            return
        context = get_current_build_context()
        assert context is not None and context.plan_id is not None
        registry.task_upload_artifacts(
            context.plan_id,
            task_id,
            [
                JSONArtifact(
                    name=TICK_DEATH_ARTIFACT_NAME,
                    body={"execution_id": str(execution_id)},
                )
            ],
            execution_id=execution_id,
        )
    except Exception as e:
        _say(
            f"SpawnDiesWithTheTick {task.id}: could not record the death ({e!r}); "
            "spawning instead"
        )
        return
    _say(
        f"SpawnDiesWithTheTick {task.id}: the tick exits hard after claiming "
        f"execution {execution_id}, before spawning it"
    )
    os._exit(137)


def install_reconcile_faults() -> None:
    """Wrap the three SDK methods the faults live in. Idempotent."""
    from stardag.artifact import JSONArtifact
    from stardag.integration.modal._executor import ModalTaskExecutor
    from stardag.integration.modal._reporter import _WorkerLifecycleReporter

    if getattr(ModalTaskExecutor.submit_detached, _INSTALLED, False):
        return

    original_submit = ModalTaskExecutor.submit_detached
    original_completed = _WorkerLifecycleReporter.completed
    original_failed = _WorkerLifecycleReporter.failed

    async def submit_detached(self, task, *, execution_id):
        # The tick has claimed ``task`` as ``execution_id`` and not yet
        # spawned anything: the window a dead tick leaves behind (STA-94).
        if isinstance(task, SpawnDiesWithTheTick):
            await asyncio.to_thread(_die_unless_died_before, task, execution_id)
        return await original_submit(self, task, execution_id=execution_id)

    def completed(self) -> None:
        # The run wrote its output; its end report is what goes missing
        # (STA-135). Replaced, not raised: the call has to *return*.
        if not isinstance(self.task, EndGoesUnreported):
            return original_completed(self)
        try:
            self.registry.task_upload_artifacts(
                self.plan_id,
                self._task_id,
                [
                    JSONArtifact(
                        name=UNREPORTED_END_ARTIFACT_NAME,
                        body={"execution_id": str(self.execution_id)},
                    )
                ],
                execution_id=self.execution_id,
            )
        except Exception as e:
            _say(f"EndGoesUnreported {self._task_id}: could not record ({e!r})")
        _say(
            f"EndGoesUnreported {self._task_id}: execution {self.execution_id} "
            "ran and returns without reporting its end"
        )

    def failed(self, exception: BaseException) -> None:
        # The attempt raised; its failure report is what goes missing, so
        # the ledger keeps the execution running while Modal retries the
        # call (STA-146). The task records its own attempts.
        if not isinstance(self.task, FailsOnceUnreported):
            return original_failed(self, exception)
        _say(
            f"FailsOnceUnreported {self._task_id}: execution "
            f"{self.execution_id} raised {exception!r}; its failure goes "
            "unreported"
        )
        # A lost report still wakes the scheduler, as ``failed`` does.
        self._guard(self._wake_scheduler, "wake")

    setattr(submit_detached, _INSTALLED, True)
    ModalTaskExecutor.submit_detached = submit_detached  # type: ignore[method-assign]
    _WorkerLifecycleReporter.completed = completed  # type: ignore[method-assign]
    _WorkerLifecycleReporter.failed = failed  # type: ignore[method-assign]
