"""The execution-ledger routes of :class:`~stardag.registry.APIRegistry`
over HTTP (the seams of
:class:`~stardag.registry._base_ledger.RegistryLedgerABC`). Split from
``_api_registry.py`` by the module-size rule; each route is one
:class:`Request`, as there.
"""

from __future__ import annotations

from uuid import UUID

from stardag.registry._api_http import HTTPTransport
from stardag.registry._api_routes import _executions_req, _stopped_req
from stardag.registry._base_ledger import RegistryLedgerABC
from stardag.registry._models import ExecutionInfo, StopOutcome, TransitionResult


class APIRegistryLedger(HTTPTransport, RegistryLedgerABC):
    """See the module docstring."""

    def build_list_executions(
        self,
        build_id: UUID,
        *,
        not_in_current_plan: bool = False,
        include_ended: bool = False,
    ) -> list[ExecutionInfo]:
        return self.call(_executions_req(build_id, not_in_current_plan, include_ended))

    async def build_list_executions_aio(
        self,
        build_id: UUID,
        *,
        not_in_current_plan: bool = False,
        include_ended: bool = False,
    ) -> list[ExecutionInfo]:
        return await self.acall(
            _executions_req(build_id, not_in_current_plan, include_ended)
        )

    def execution_report_stopped(
        self,
        execution_id: UUID,
        *,
        outcome: StopOutcome = "stopped",
        only_if_unspawned: bool = False,
    ) -> TransitionResult:
        return self.call(_stopped_req(execution_id, outcome, only_if_unspawned))

    async def execution_report_stopped_aio(
        self,
        execution_id: UUID,
        *,
        outcome: StopOutcome = "stopped",
        only_if_unspawned: bool = False,
    ) -> TransitionResult:
        return await self.acall(_stopped_req(execution_id, outcome, only_if_unspawned))
