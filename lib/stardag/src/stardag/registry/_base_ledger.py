"""The execution-ledger seams of :class:`~stardag.registry.RegistryABC`:
listing a build's executions and an operator's (or a reconciling tick's)
end of one. Split from ``_base.py`` by the module-size rule;
:class:`RegistryABC` inherits them, and the ``_aio`` defaults call the sync
method, as there.
"""

from __future__ import annotations

from uuid import UUID

from stardag.registry._base_reads import _missing
from stardag.registry._models import ExecutionInfo, StopOutcome, TransitionResult


class RegistryLedgerABC:
    """See the module docstring."""

    def build_list_executions(
        self,
        build_id: UUID,
        *,
        not_in_current_plan: bool = False,
        include_ended: bool = False,
    ) -> list[ExecutionInfo]:
        """``GET /builds/{id}/executions``: the build's executions with no end
        reported (``builds stop``, a worker's cancellation checkpoint);
        ``not_in_current_plan`` keeps the orphans; ``include_ended`` lists
        the whole ledger (every execution granted, ended or not)."""
        raise _missing(self, "build_list_executions")

    async def build_list_executions_aio(
        self,
        build_id: UUID,
        *,
        not_in_current_plan: bool = False,
        include_ended: bool = False,
    ) -> list[ExecutionInfo]:
        return self.build_list_executions(
            build_id,
            not_in_current_plan=not_in_current_plan,
            include_ended=include_ended,
        )

    def execution_report_stopped(
        self,
        execution_id: UUID,
        *,
        outcome: StopOutcome = "stopped",
        only_if_unspawned: bool = False,
    ) -> TransitionResult:
        """``POST /executions/{id}/stopped``: an operator ends an execution —
        ``stopped`` (its call was cancelled) or ``lost`` (it could not be,
        and no report of it will ever be applied). If it is the task's
        current execution with its claim unreleased, the claim is released
        ``cancelled`` and the task is CANCELLED (a revocation is not a
        result).

        ``only_if_unspawned``: applied only while the execution has recorded
        no executor ref, else refused 409 ``execution_spawned`` (a
        reconciling tick, STA-94). A server that predates the condition
        refuses it 422 rather than ignoring it."""
        raise _missing(self, "execution_report_stopped")

    async def execution_report_stopped_aio(
        self,
        execution_id: UUID,
        *,
        outcome: StopOutcome = "stopped",
        only_if_unspawned: bool = False,
    ) -> TransitionResult:
        return self.execution_report_stopped(
            execution_id, outcome=outcome, only_if_unspawned=only_if_unspawned
        )
