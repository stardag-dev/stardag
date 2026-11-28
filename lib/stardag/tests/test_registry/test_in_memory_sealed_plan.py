"""The in-memory registry's sealed-plan rules follow the server's
(``_check_sealed`` and the expanded-edge rule of ``POST /plans/{id}/members``),
so the fake cannot accept a registration the registry refuses."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stardag.build._registration import new_id
from stardag.exceptions import APIError
from stardag.registry import RegistrationItem
from stardag.testing import InMemoryRegistry


def _item(
    task_id: str,
    upstreams: list[str] | None = None,
    *,
    observed_complete: bool = False,
) -> RegistrationItem:
    return RegistrationItem(
        task_id=task_id,
        task_namespace="ns",
        task_name="T",
        instance_hash=f"h-{task_id}",
        body={"__namespace": "ns", "__name": "T", "id": task_id},
        declared_upstreams=upstreams,
        observed_complete=observed_complete,
        observed_at=datetime.now(timezone.utc),
    )


def _sealed(registry: InMemoryRegistry, *members: RegistrationItem):
    """A sealed plan rooted at ``R`` holding ``members``."""
    deployment_id = registry.add_deployment()
    build = registry.build_create(root_task_ids=["R"])
    plan = registry.plan_create(
        build.id,
        plan_id=new_id(),
        deployment_id=deployment_id,
        settings={},
        roots=[_item("R")],
    )
    registry.plan_register_members(plan.id, list(members))
    registry.plan_seal(plan.id)
    return plan.id, deployment_id


def _upstreams(registry: InMemoryRegistry, plan_id, task_id: str) -> set[str]:
    member = registry.members[plan_id][task_id]
    return {
        registry.instances[u].task_id
        for u in registry.instances[member.instance_id].upstreams
    }


def test_a_sealed_plan_refuses_new_members_and_new_edges():
    """Mirrors the API test of the same name: an exact re-delivery is a
    no-op, a member the plan does not hold is ``plan_sealed``, and so is a
    new edge on an instance the plan already expanded -- with nothing of
    the refused chunk applied."""
    registry = InMemoryRegistry()
    a, c, root = _item("A", []), _item("C", []), _item("R", ["h-A"])
    plan_id, _ = _sealed(registry, a, c, root)

    again = registry.plan_register_members(plan_id, [a, c, root])
    assert again.members_admitted == again.edges_created == 0

    with pytest.raises(APIError) as excinfo:
        registry.plan_register_members(plan_id, [_item("B", [])])
    assert excinfo.value.code == "plan_sealed"
    assert "B" not in registry.tasks

    grown = _item("R", ["h-A", "h-C"])
    with pytest.raises(APIError) as excinfo:
        # A new member alongside, to show the chunk is refused as a whole.
        registry.plan_register_members(plan_id, [c, grown])
    assert excinfo.value.code == "plan_sealed"
    assert (excinfo.value.payload or {}).get("task_ids") == ["R"]
    assert _upstreams(registry, plan_id, "R") == {"A"}
    assert set(registry.members[plan_id]) == {"A", "C", "R"}


def test_a_sealed_plan_takes_a_discovery_jobs_result():
    """A root admitted unexpanded is expanded for the first time through
    ``/members`` on the sealed plan, with the new upstream it reaches."""
    registry = InMemoryRegistry()
    plan_id, _ = _sealed(registry, _item("R", None, observed_complete=True))
    registry.plan_register_members(plan_id, [_item("R", None)])

    result = registry.plan_register_members(
        plan_id, [_item("U", []), _item("R", ["h-U"])]
    )
    assert result.edges_created == 1
    assert "U" in registry.members[plan_id]
    assert _upstreams(registry, plan_id, "R") == {"U"}


def test_a_yield_may_grow_an_expanded_instances_edges_on_a_sealed_plan():
    """``/yield`` registers into a sealed plan by design, edges on an
    already-expanded instance included."""
    registry = InMemoryRegistry()
    a, c, root = _item("A", []), _item("C", []), _item("R", ["h-A"])
    plan_id, deployment_id = _sealed(registry, a, c, _item("P", []), root)
    execution_id = new_id()
    registry.member_start(plan_id, "P", execution_id=execution_id)

    registry.member_yield(
        plan_id,
        "P",
        execution_id=execution_id,
        deployment_id=deployment_id,
        batch_id=new_id(),
        items=[_item("R", ["h-A", "h-C"])],
        yielded=["h-R"],
        suspend=True,
    )
    assert _upstreams(registry, plan_id, "R") == {"A", "C"}


@pytest.mark.parametrize(
    "upstreams", [["h-A", "h-C", "h-missing"], ["h-missing", "h-C", "h-A"]]
)
def test_an_unknown_upstream_is_refused_as_such_in_any_order(upstreams):
    """The registry resolves every declared upstream of a chunk before the
    sealed-plan edge rule, so an unknown one is ``unknown_upstream`` even
    beside a new (known) upstream of an expanded instance, in either order;
    nothing of the chunk lands."""
    registry = InMemoryRegistry()
    a, c, root = _item("A", []), _item("C", []), _item("R", ["h-A"])
    plan_id, _ = _sealed(registry, a, c, root)

    with pytest.raises(APIError) as excinfo:
        registry.plan_register_members(plan_id, [_item("R", upstreams)])
    assert excinfo.value.code == "unknown_upstream"
    assert (excinfo.value.payload or {}).get("instance_hashes") == ["h-missing"]
    assert _upstreams(registry, plan_id, "R") == {"A"}
