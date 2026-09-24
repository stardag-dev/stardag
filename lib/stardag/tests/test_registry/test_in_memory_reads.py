"""The in-memory registry's inspecting reads follow the server's shapes and
orderings (``testing/_registry_reads.py``)."""

from __future__ import annotations

import pytest

from stardag.build._registration import new_id
from stardag.exceptions import NotFoundError
from stardag.registry import RegistrationItem
from stardag.testing import InMemoryRegistry


def _item(task_id: str, upstreams: list[str] | None = None) -> RegistrationItem:
    from datetime import datetime, timezone

    return RegistrationItem(
        task_id=task_id,
        task_namespace="ns",
        task_name="T",
        instance_hash=f"h-{task_id}",
        body={"__namespace": "ns", "__name": "T", "id": task_id},
        declared_upstreams=upstreams,
        observed_at=datetime.now(timezone.utc),
    )


def _build(registry: InMemoryRegistry):
    deployment_id = registry.add_deployment()
    build = registry.build_create(root_task_ids=["root"])
    plan = registry.plan_create(
        build.id,
        plan_id=new_id(),
        deployment_id=deployment_id,
        settings={},
        roots=[_item("root")],
    )
    registry.plan_register_members(
        plan.id, [_item("leaf", []), _item("mid", ["h-leaf"]), _item("root", ["h-mid"])]
    )
    return build.id, plan.id, deployment_id


def test_plan_get_counts_members_by_status_and_excluded_apart():
    registry = InMemoryRegistry()
    build_id, plan_id, deployment_id = _build(registry)
    registry.tasks["leaf"].status = "failed"
    registry.member_exclude(plan_id, "mid", reason="given up")
    plan = registry.plan_get(plan_id)
    assert plan.member_count == 3 and plan.root_count == 1
    assert plan.excluded_count == 2  # mid and its downstream root
    assert plan.member_counts == {"failed": 1}
    assert plan.deployment is not None and plan.deployment.id == deployment_id
    assert plan.created_at is not None
    assert registry.build_list_plans(build_id)[0].id == plan_id
    with pytest.raises(NotFoundError):
        registry.plan_get(new_id())


def test_build_list_pages_with_a_total():
    registry = InMemoryRegistry()
    for _ in range(3):
        registry.build_create(root_task_ids=["t"])
    first = registry.build_list_page(limit=2)
    assert (len(first.builds), first.total) == (2, 3)
    assert first.next_cursor is not None
    second = registry.build_list_page(limit=2, cursor=first.next_cursor)
    assert (len(second.builds), second.next_cursor) == (1, None)
    assert {b.id for b in first.builds}.isdisjoint({b.id for b in second.builds})


def test_a_failed_builds_reason_is_served():
    registry = InMemoryRegistry()
    build = registry.build_create(root_task_ids=["t"])
    registry.build_fail(build.id, "stalled")
    assert registry.build_get(build.id).error_message == "stalled"


def test_the_claim_holder_executions_and_events_of_a_task():
    registry = InMemoryRegistry()
    build_id, plan_id, _ = _build(registry)
    execution_id = new_id()
    registry.member_start(plan_id, "leaf", execution_id=execution_id)
    task = registry.task_get("leaf")
    assert (task.claim_plan_id, task.claim_build_id) == (plan_id, build_id)
    (listed,) = [t for t in registry.task_list(status="running").tasks]
    assert listed.task_id == "leaf" and listed.claim_build_id == build_id
    assert [e.id for e in registry.task_list_executions("leaf")] == [execution_id]
    registry.member_complete(plan_id, "leaf", execution_id=execution_id)
    assert registry.task_get("leaf").claim_build_id is None
    assert registry.task_list_executions("leaf", include_ended=False) == []
    (row,) = registry.task_list_executions("leaf")
    assert (row.build_id, row.outcome) == (build_id, "completed")
    events = registry.task_events("leaf")
    assert [e.event_type for e in events][-2:] == ["task_started", "task_completed"]
    assert all(e.id is not None and e.created_at is not None for e in events)
    failing = new_id()
    registry.member_retry(plan_id, "mid")  # a no-op on PENDING
    registry.member_start(plan_id, "mid", execution_id=failing)
    registry.member_fail(plan_id, "mid", execution_id=failing, error_message="boom")
    assert registry.task_events("mid")[-1].error_message == "boom"


def test_deployment_get():
    registry = InMemoryRegistry()
    deployment_id = registry.add_deployment()
    assert registry.deployment_get(deployment_id).is_current
    with pytest.raises(NotFoundError):
        registry.deployment_get(new_id())
