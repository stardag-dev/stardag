"""The dynamic phase of :class:`~stardag.testing.InMemoryRegistry`:
``/yield`` -- a running parent's children with their closure, the dynamic
edges and optionally its suspend, in one transaction, replayed by
``(execution_id, batch_id)`` (design.md, "Dynamic phase")."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from stardag.registry import RegistrationItem, YieldResult
from stardag.testing._registry_plans import PlansMixin
from stardag.testing._registry_state import Event, refuse


class YieldMixin(PlansMixin):
    def member_yield(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        deployment_id: UUID,
        batch_id: UUID,
        items: Sequence[RegistrationItem],
        yielded: Sequence[str],
        suspend: bool,
    ) -> YieldResult:
        self._record(
            "member_yield",
            plan_id=plan_id,
            task_id=task_id,
            execution_id=execution_id,
            deployment_id=deployment_id,
            batch_id=batch_id,
            items=list(items),
            yielded=list(yielded),
            suspend=suspend,
        )
        replay = self.yields.get((execution_id, batch_id))
        if replay is not None:
            return replay.model_copy(update={"replayed": True})
        with self.transaction():
            plan = self.plan(plan_id)
            if deployment_id != plan.deployment_id:
                raise refuse("deployment_mismatch")
            task = self.task(task_id)
            execution = self.executions.get(execution_id)
            if (
                execution is None
                or task.execution_id != execution_id
                or execution.claim_released_at is not None
            ):
                raise refuse("execution_not_current")
            self._check_claim_plan(task, plan_id)
            hashes = {i.instance_hash for i in items}
            if not set(yielded) <= hashes:
                raise refuse("yielded_not_in_items", status=400)
            members = self._register_items(
                plan, items, as_roots=False, dynamic=set(yielded)
            )
            parent = self.instances[self.member(plan_id, task_id).instance_id]
            edges = 0
            for child_hash in yielded:
                child_id = self.instance_index[
                    (plan.deployment_id, plan.settings_hash, child_hash)
                ]
                if child_id not in parent.upstreams:
                    parent.upstreams[child_id] = True
                    edges += 1
            self.log(
                Event("TASK_YIELDED", task_id, plan.build_id, plan_id, execution_id)
            )
            if suspend:
                self._report(
                    plan_id,
                    task_id,
                    execution_id,
                    status="suspended",
                    outcome="suspended",
                )
            result = YieldResult(
                members=members,
                dynamic_edges_created=edges,
                status=task.status,
                execution_id=task.execution_id,
                claim_expires_at=task.claim_expires_at,
            )
            self.yields[(execution_id, batch_id)] = result
            return result
