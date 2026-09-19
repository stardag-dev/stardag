"""The structure scope: what a build's dependency edges are keyed by.

Edges are facts about the code and structure config that evaluated them,
not about the task id, so every edge carries the ``scope_key`` of the build
that registered it and readiness is evaluated over a build's own scope only.
The build's scope is fixed once, by whoever runs discovery, before any edge
is written. See ``docs/design/scope-keyed-dependency-structure.md``.

Pinned here:

- the set-once rule on ``PUT /builds/{id}/scope`` and on resume;
- gating and closure read one scope and no other;
- a stalled build re-closes its plan over the scope, so an edge a
  scope-mate wrote after registration is picked up rather than waited on;
- CANCELLED and SKIPPED are actionable exactly when gated open.
"""

import pytest
from httpx import AsyncClient
from uuid import uuid4

BUILDS = "/api/v1/builds"


def _register(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
        "dependency_task_ids": deps or [],
    }


async def _build(client: AsyncClient, scope: str | None = None, **extra) -> dict:
    body: dict = dict(extra)
    if scope is not None:
        body["scope_key"] = scope
    response = await client.post(BUILDS, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _register_task(
    client: AsyncClient, build_id: str, task_id: str, deps: list[str] | None = None
) -> None:
    response = await client.post(
        f"{BUILDS}/{build_id}/tasks", json=_register(task_id, deps)
    )
    assert response.status_code == 201, response.text


async def _frontier(client: AsyncClient, build_id: str) -> dict:
    return (await client.get(f"{BUILDS}/{build_id}/frontier")).json()


def _actionable(frontier: dict) -> dict[str, dict]:
    return {t["task_id"]: t for t in frontier["actionable"]}


# --- Set-once ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_build_carries_the_synthetic_scope(client: AsyncClient):
    """Until something sets a real scope, a build's scope names itself —
    nobody else shares it, so an SDK that never sets one gets per-build
    edges: no caching, always correct."""
    build = await _build(client)
    assert build["scope_key"] == f"build:{build['id']}"
    assert build["build_config"] is None


@pytest.mark.asyncio
async def test_create_with_a_scope_uses_it(client: AsyncClient):
    build = await _build(client, "code-1:cfg-a", build_config={"ns.T": {"n": 1}})
    assert build["scope_key"] == "code-1:cfg-a"
    assert build["build_config"] == {"ns.T": {"n": 1}}
    fetched = (await client.get(f"{BUILDS}/{build['id']}")).json()
    assert fetched["scope_key"] == "code-1:cfg-a"
    assert fetched["build_config"] == {"ns.T": {"n": 1}}


@pytest.mark.asyncio
async def test_set_scope_replaces_the_synthetic_one_and_is_then_fixed(
    client: AsyncClient,
):
    """Synthetic → set; the same scope again is a no-op; a different one is
    refused. A build carries one scope for its life."""
    build_id = (await _build(client))["id"]

    response = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-a", "build_config": {"ns.T": {"n": 1}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope_key"] == "code-1:cfg-a"
    assert response.json()["build_config"] == {"ns.T": {"n": 1}}

    # Idempotent re-trigger: same scope, same config.
    response = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-a", "build_config": {"ns.T": {"n": 1}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope_key"] == "code-1:cfg-a"

    # Other code: a new build, not this one.
    response = await client.put(
        f"{BUILDS}/{build_id}/scope", json={"scope_key": "code-2:cfg-a"}
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "scope_mismatch"
    assert detail["scope_key"] == "code-1:cfg-a"
    assert detail["requested_scope_key"] == "code-2:cfg-a"

    # Same scope, other config: also refused — a build has one config.
    response = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-a", "build_config": {"ns.T": {"n": 2}}},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "scope_mismatch"

    # Nothing moved.
    fetched = (await client.get(f"{BUILDS}/{build_id}")).json()
    assert fetched["scope_key"] == "code-1:cfg-a"
    assert fetched["build_config"] == {"ns.T": {"n": 1}}


@pytest.mark.asyncio
async def test_resume_checks_the_scope(client: AsyncClient):
    """A re-trigger under another scope is refused; under the same one it
    proceeds. Without a scope on the query the check is skipped (older
    SDKs)."""
    build_id = (await _build(client, "code-1:cfg-a"))["id"]
    await _register_task(client, build_id, "t")

    mismatch = await client.post(
        f"{BUILDS}/{build_id}/resume", params={"scope_key": "code-2:cfg-a"}
    )
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["detail"]["error_code"] == "scope_mismatch"

    same = await client.post(
        f"{BUILDS}/{build_id}/resume", params={"scope_key": "code-1:cfg-a"}
    )
    assert same.status_code == 200, same.text
    assert same.json()["scope_key"] == "code-1:cfg-a"

    bare = await client.post(f"{BUILDS}/{build_id}/resume")
    assert bare.status_code == 200, bare.text


@pytest.mark.asyncio
async def test_resume_compares_the_config_against_nothing_stored(
    client: AsyncClient,
):
    """A build with a real scope and no stored config is a build whose config
    is empty, not one whose config is unknown: resuming it with overrides
    would change an already-running build's config under the same scope
    (execution-only overrides leave the scope hash unchanged), so that is a
    409. An explicit ``{}`` is the same config; no ``build_config`` at all
    means unspecified and keeps what is stored."""
    build_id = (await _build(client, "code-1:cfg-a"))["id"]
    await _register_task(client, build_id, "t")

    changed = await client.post(
        f"{BUILDS}/{build_id}/resume",
        params={"scope_key": "code-1:cfg-a", "build_config": '{"ns.T": {"n": 2}}'},
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["error_code"] == "scope_mismatch"

    empty = await client.post(
        f"{BUILDS}/{build_id}/resume",
        params={"scope_key": "code-1:cfg-a", "build_config": "{}"},
    )
    assert empty.status_code == 200, empty.text

    unspecified = await client.post(
        f"{BUILDS}/{build_id}/resume", params={"scope_key": "code-1:cfg-a"}
    )
    assert unspecified.status_code == 200, unspecified.text
    assert not unspecified.json()["build_config"]


@pytest.mark.asyncio
async def test_resume_adopts_a_scope_onto_a_synthetic_build(client: AsyncClient):
    build_id = (await _build(client))["id"]
    response = await client.post(
        f"{BUILDS}/{build_id}/resume",
        params={"scope_key": "code-9:cfg", "build_config": '{"ns.T": {"n": 3}}'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope_key"] == "code-9:cfg"
    assert response.json()["build_config"] == {"ns.T": {"n": 3}}


# --- The synthetic shape is the server's -------------------------------


@pytest.mark.asyncio
async def test_a_claimed_scope_may_not_look_synthetic(client: AsyncClient):
    """Ticks and workers read ``build:<uuid>`` as the server's placeholder
    and skip the code-id guard for it, so a client must not be able to
    claim that shape — on create, on resume, or on the scope route."""
    other = uuid4()
    created = await client.post(BUILDS, json={"scope_key": f"build:{other}"})
    assert created.status_code == 400, created.text
    assert created.json()["detail"]["error_code"] == "synthetic_scope_claimed"

    build_id = (await _build(client))["id"]
    for claimed in (f"build:{other}", f"build:{build_id}", "build:ffff"):
        put = await client.put(
            f"{BUILDS}/{build_id}/scope", json={"scope_key": claimed}
        )
        assert put.status_code == 400, (claimed, put.text)
        assert put.json()["detail"]["error_code"] == "synthetic_scope_claimed"
        resumed = await client.post(
            f"{BUILDS}/{build_id}/resume", params={"scope_key": claimed}
        )
        assert resumed.status_code == 400, (claimed, resumed.text)
        assert resumed.json()["detail"]["error_code"] == "synthetic_scope_claimed"

    # The build kept the scope the server gave it, and a real claim still works.
    info = (await client.get(f"{BUILDS}/{build_id}")).json()
    assert info["scope_key"] == f"build:{build_id}"
    fixed = await client.put(
        f"{BUILDS}/{build_id}/scope", json={"scope_key": "code:cfg"}
    )
    assert fixed.status_code == 200, fixed.text


# --- Gating reads one scope --------------------------------------------


@pytest.mark.asyncio
async def test_gating_reads_only_the_builds_own_scope(client: AsyncClient):
    """The same task, two scopes, two upstream sets. Each build is gated by
    its own declaration and sees nothing of the other's."""
    build_a = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_a, "u1")
    await _register_task(client, build_a, "t", ["u1"])

    build_b = (await _build(client, "code-b:cfg"))["id"]
    await _register_task(client, build_b, "u2")
    await _register_task(client, build_b, "t", ["u2"])

    await client.post(f"{BUILDS}/{build_a}/tasks/u1/start")
    await client.post(f"{BUILDS}/{build_a}/tasks/u1/complete")

    assert "t" in _actionable(await _frontier(client, build_a))
    frontier_b = await _frontier(client, build_b)
    assert "t" not in _actionable(frontier_b), frontier_b
    assert set(_actionable(frontier_b)) == {"u2"}


@pytest.mark.asyncio
async def test_a_scope_mates_edge_gates_a_task_registered_without_it(
    client: AsyncClient,
):
    """Within one scope the edge is what this build would have discovered,
    so it gates and its upstream is admitted — even though this build
    registered the task declaring no upstreams."""
    build_a = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_a, "u2")
    await _register_task(client, build_a, "t", ["u2"])

    build_b = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_b, "t")

    frontier_b = await _frontier(client, build_b)
    assert set(_actionable(frontier_b)) == {"u2"}, frontier_b
    assert frontier_b["status_counts"] == {"pending": 2}


# --- Stall-time closure --------------------------------------------------


@pytest.mark.asyncio
async def test_a_stalled_build_recloses_its_plan_over_the_scope(
    client: AsyncClient,
):
    """Closure runs at registration and again when the build stalls.

    B registered ``parent`` before A's worker yielded ``child``, so the edge
    was written after B closed its plan. While A's parent is RUNNING, B is
    not stalled (a running task in its plan). When A suspends the parent,
    B has nothing actionable and nothing running — and instead of reporting
    a neighbour's business, it re-closes over the scope, admits ``child``
    and runs it itself.
    """
    build_a = (await _build(client, "code-a:cfg"))["id"]
    build_b = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_a, "parent")
    await _register_task(client, build_b, "parent")

    await client.post(
        f"{BUILDS}/{build_a}/tasks/parent/start", params={"claim": "true"}
    )
    frontier_b = await _frontier(client, build_b)
    assert [t["task_id"] for t in frontier_b["running"]] == ["parent"]

    # A's worker yields a dynamic child: registers it, then posts the edge.
    await _register_task(client, build_a, "child")
    response = await client.post(
        f"{BUILDS}/{build_a}/tasks/parent/dependencies",
        json={"upstream_task_ids": ["child"], "is_dynamic": True},
    )
    assert response.status_code == 200, response.text
    await client.post(f"{BUILDS}/{build_a}/tasks/parent/suspend")

    frontier_b = await _frontier(client, build_b)
    assert frontier_b["running"] == []
    assert "child" in _actionable(frontier_b), frontier_b
    assert "parent" not in _actionable(frontier_b)
    assert frontier_b["status_counts"] == {"pending": 1, "suspended": 1}
    assert frontier_b["blocked_by_external"] == []

    events = (await client.get(f"{BUILDS}/{build_b}/events")).json()
    child_pk = next(
        n["id"]
        for n in (await client.get(f"{BUILDS}/{build_a}/graph")).json()["nodes"]
        if n["task_id"] == "child"
    )
    referenced = [
        e
        for e in events
        if e["event_type"] == "task_referenced" and e["task_id"] == child_pk
    ]
    assert referenced, events


# --- CANCELLED / SKIPPED actionable when gated open -----------------------


@pytest.mark.asyncio
async def test_skipped_is_actionable_only_once_its_upstreams_complete(
    client: AsyncClient,
):
    """A skip is derived: it marks a task downstream of a failure. Gated,
    it stays out of the frontier; once every upstream is complete, the
    reason for the skip is gone and the task is the build's to reset."""
    build_id = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_id, "up")
    await _register_task(client, build_id, "down", ["up"])

    await client.post(f"{BUILDS}/{build_id}/tasks/up/start")
    await client.post(f"{BUILDS}/{build_id}/tasks/up/fail")
    skipped = (await client.post(f"{BUILDS}/{build_id}/skip-blocked")).json()
    assert skipped["skipped_task_ids"] == ["down"]

    frontier = await _frontier(client, build_id)
    assert "down" not in _actionable(frontier), frontier

    await client.post(f"{BUILDS}/{build_id}/tasks/up/retry")
    await client.post(f"{BUILDS}/{build_id}/tasks/up/start")
    await client.post(f"{BUILDS}/{build_id}/tasks/up/complete")

    frontier = await _frontier(client, build_id)
    actionable = _actionable(frontier)
    assert list(actionable) == ["down"], frontier
    assert actionable["down"]["latest_status"] == "skipped"


@pytest.mark.asyncio
async def test_cancelled_is_actionable_only_once_its_upstreams_complete(
    client: AsyncClient,
):
    build_id = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_id, "up")
    await _register_task(client, build_id, "down", ["up"])
    await client.post(f"{BUILDS}/{build_id}/tasks/down/cancel")

    frontier = await _frontier(client, build_id)
    assert "down" not in _actionable(frontier), frontier

    await client.post(f"{BUILDS}/{build_id}/tasks/up/start")
    await client.post(f"{BUILDS}/{build_id}/tasks/up/complete")

    frontier = await _frontier(client, build_id)
    actionable = _actionable(frontier)
    assert list(actionable) == ["down"], frontier
    assert actionable["down"]["latest_status"] == "cancelled"
