"""``builds stop``, orphans, and the v2 route guardrails (``api-pg`` tier).

Written from design.md, "Rollover" (orphaned executions, ``builds stop``),
the ``execution`` entity, and "Peripheral tables, re-pointed" (the 24-hour
creation quota counts ``task_instance`` rows).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from httpx import AsyncClient

from stardag_api.config import limits_settings
from stardag_api.limits import _rate_limiter
from stardag_api.services import builds, executions
from stardag_api.services.errors import TooManyRequests
from tests.v2_support import ENV, Harness, item, observed


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


@pytest.fixture
def instance_quota(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setattr(limits_settings, "max_task_instances_per_environment_24h", 3)
    yield 3


@pytest.fixture
def rate_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setattr(limits_settings, "max_requests_per_minute", 2)
    _rate_limiter.clear()
    yield 2
    _rate_limiter.clear()


async def _unended(h: Harness, build, **kwargs) -> list[executions.ExecutionState]:
    async with h.sf() as s:
        return await executions.list_unended(s, ENV, build, **kwargs)


async def _stopped(h: Harness, execution_id):
    async with h.sf() as s:
        return await executions.report_stopped(s, ENV, execution_id)


async def test_orphans_are_unended_executions_outside_the_active_plan(h: Harness):
    """An orphan is an execution with ``ended_at IS NULL`` whose plan is not
    the build's active plan; ``builds stop`` lists every unended execution,
    ``--not-in-current-plan`` only the orphans."""
    deployment = await h.new_deployment()
    a, b = item("A"), item("B")
    root = item("Root", upstreams=[a, b])
    build = await h.new_build([root])
    old = await h.plan(build, deployment, [root])
    await h.register(old.id, [a, b, root])
    await h.seal(old.id)
    orphan = await h.start(old.id, a)
    new = await h.plan(build, deployment, [root], settings={"S": "2"})
    await h.register(new.id, [a, b, root])
    await h.seal(new.id)
    current = await h.start(new.id, b)

    listed = await _unended(h, build)
    assert {e.id for e in listed} == {orphan, current}
    assert {e.id: e.in_current_plan for e in listed} == {
        orphan: False,
        current: True,
    }
    (only,) = await _unended(h, build, not_in_current_plan=True)
    assert only.id == orphan and only.task_id == a.task_id


async def test_a_stopped_execution_is_ended_and_its_claim_released(h: Harness):
    """``/executions/{id}/stopped`` writes ``ended_at`` with ``outcome =
    stopped``; an execution that still holds the task's claim will never
    report, so the claim is released too (``claim_outcome = cancelled``,
    the task CANCELLED and ACTIONABLE). Idempotent; its late report is
    refused; the build can then be deleted."""
    t = item("T")
    build, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    stopped = await _stopped(h, execution)
    assert stopped.applied and stopped.status == "cancelled"
    ledger = await h.execution(execution)
    assert (ledger["outcome"], ledger["claim_outcome"]) == ("stopped", "cancelled")
    assert await _unended(h, build) == []
    assert not (await _stopped(h, execution)).applied

    async with h.sf() as s:
        await builds.delete_build(s, ENV, build)
    assert await h.count("build") == 0


async def test_stopping_an_execution_whose_claim_moved_leaves_the_task(
    h: Harness,
):
    """A stopped execution whose lapsed claim was taken over only gets its
    ledger end; the successor's claim is untouched."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    dead = await h.start(plan.id, t)
    await h.lapse_claim(t)
    successor = await h.start(plan.id, t)
    await _stopped(h, dead)
    assert (await h.execution(dead))["outcome"] == "stopped"
    task = await h.task(t)
    assert task["status"] == "running" and task["execution_id"] == successor


async def test_the_creation_quota_counts_inserted_instances_only(
    h: Harness, instance_quota: int
):
    """The 24-hour quota counts ``task_instance`` rows per environment and
    charges a chunk only for the rows it inserted: a re-delivered chunk is
    never refused; a chunk that would go over is refused 429
    ``creation_quota_exceeded`` and lands nothing."""
    a, b = item("A"), item("B")
    root = item("Root", upstreams=[a, b])
    _, plan = await h.planned([root], [a, b, root])  # 3 instances: the quota
    assert await h.count("task_instance") == instance_quota
    again = await h.register(plan.id, [a, b, root, observed(a, False)])
    assert again.instances_created == 0

    with pytest.raises(TooManyRequests) as exc:
        await h.register(plan.id, [item("One"), item("More")])
    assert exc.value.code == "creation_quota_exceeded"
    assert exc.value.detail["requested"] == 2
    assert await h.count("task_instance") == instance_quota


async def test_the_creation_quota_over_http(
    client: AsyncClient, h: Harness, instance_quota: int
):
    root = item("Root")
    _, plan = await h.planned([root], [root])
    response = await client.post(
        f"/api/v2/plans/{plan.id}/members",
        json={"items": [item(n).model_dump(mode="json") for n in ("X1", "X2", "X3")]},
    )
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "creation_quota_exceeded"


async def test_every_v2_write_route_is_rate_limited(
    client: AsyncClient, rate_limit: int
):
    """Smoke test of the router-level guard: writes past the per-workspace
    limit are 429 ``rate_limited`` with ``Retry-After``; reads are not
    limited."""
    for _ in range(rate_limit):
        created = await client.post(
            "/api/v2/builds", json={"root_task_ids": ["t"], "id": str(uuid4())}
        )
        assert created.status_code == 200, created.text
    limited = await client.post(f"/api/v2/builds/{uuid4()}/cancel")
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    read = await client.get(f"/api/v2/builds/{uuid4()}/executions")
    assert read.status_code == 404  # not limited: it reached the service


async def test_executions_over_http(client: AsyncClient, h: Harness):
    t = item("T")
    build, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    listed = await client.get(f"/api/v2/builds/{build}/executions")
    assert listed.status_code == 200
    assert [e["id"] for e in listed.json()["executions"]] == [str(execution)]
    orphans = await client.get(
        f"/api/v2/builds/{build}/executions", params={"not_in_current_plan": True}
    )
    assert orphans.json()["executions"] == []
    stopped = await client.post(
        f"/api/v2/executions/{execution}/stopped", json={"outcome": "stopped"}
    )
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    unknown = await client.post(f"/api/v2/executions/{uuid4()}/stopped")
    assert unknown.status_code == 404
