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
async def test_set_scope_replaces_the_synthetic_one_and_moves_to_new_code(
    client: AsyncClient,
):
    """Synthetic → set; the same scope again is a no-op; another real scope
    is a rollover to new code; another config is refused. A build's scope is
    the one it is currently planned under; its config is for life."""
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

    # Other config: refused — a build has one config.
    response = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-a", "build_config": {"ns.T": {"n": 2}}},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "scope_mismatch"
    fetched = (await client.get(f"{BUILDS}/{build_id}")).json()
    assert fetched["scope_key"] == "code-1:cfg-a"
    assert fetched["build_config"] == {"ns.T": {"n": 1}}

    # Other code, same config: the build rolls over.
    response = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-2:cfg-a", "build_config": {"ns.T": {"n": 1}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope_key"] == "code-2:cfg-a"
    assert response.json()["build_config"] == {"ns.T": {"n": 1}}


@pytest.mark.asyncio
async def test_resume_sets_or_moves_the_scope(client: AsyncClient):
    """A re-trigger under the same scope proceeds; under other code it
    moves the build there; without a scope it keeps the current one."""
    build_id = (await _build(client, "code-1:cfg-a"))["id"]
    await _register_task(client, build_id, "t")

    same = await client.post(
        f"{BUILDS}/{build_id}/resume", params={"scope_key": "code-1:cfg-a"}
    )
    assert same.status_code == 200, same.text
    assert same.json()["scope_key"] == "code-1:cfg-a"

    moved = await client.post(
        f"{BUILDS}/{build_id}/resume", params={"scope_key": "code-2:cfg-a"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["scope_key"] == "code-2:cfg-a"

    bare = await client.post(f"{BUILDS}/{build_id}/resume")
    assert bare.status_code == 200, bare.text
    assert bare.json()["scope_key"] == "code-2:cfg-a"


@pytest.mark.asyncio
async def test_a_bare_resume_of_a_synthetic_build_is_the_older_sdk_path(
    client: AsyncClient,
):
    """A build nothing scoped has no structure to protect; an older SDK
    re-triggers it as it always did."""
    build_id = (await _build(client))["id"]
    await _register_task(client, build_id, "t")
    bare = await client.post(f"{BUILDS}/{build_id}/resume")
    assert bare.status_code == 200, bare.text
    assert bare.json()["scope_key"] == f"build:{build_id}"


@pytest.mark.asyncio
async def test_an_explicit_empty_config_is_a_config(client: AsyncClient):
    """``{}`` stored at POST /builds means "no overrides", and it is fixed
    like any other config: a later scope claim may not bring overrides in.
    Only a build created with no config at all adopts the first claim's."""
    build_id = (await _build(client, build_config={}))["id"]

    overrides = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-b", "build_config": {"ns.T": {"n": 1}}},
    )
    assert overrides.status_code == 409, overrides.text
    assert overrides.json()["detail"]["error_code"] == "scope_mismatch"
    assert (await client.get(f"{BUILDS}/{build_id}")).json()["build_config"] == {}

    same = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-a", "build_config": {}},
    )
    assert same.status_code == 200, same.text
    assert same.json()["build_config"] == {}

    # No config at all at creation: the first claim's config is adopted.
    bare_id = (await _build(client))["id"]
    adopted = await client.put(
        f"{BUILDS}/{bare_id}/scope",
        json={"scope_key": "code-1:cfg-b", "build_config": {"ns.T": {"n": 1}}},
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["build_config"] == {"ns.T": {"n": 1}}


@pytest.mark.asyncio
async def test_a_stored_config_is_fixed_before_the_scope_is(client: AsyncClient):
    """A reactive trigger stores the config at POST /builds while the scope
    is still synthetic; the bootstrap's later scope claim may confirm that
    config but not rewrite it."""
    build_id = (await _build(client, build_config={"ns.T": {"width": 3}}))["id"]

    other = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-b", "build_config": {"ns.T": {"width": 5}}},
    )
    assert other.status_code == 409, other.text
    assert other.json()["detail"]["error_code"] == "scope_mismatch"

    info = (await client.get(f"{BUILDS}/{build_id}")).json()
    assert info["scope_key"] == f"build:{build_id}"
    assert info["build_config"] == {"ns.T": {"width": 3}}

    same = await client.put(
        f"{BUILDS}/{build_id}/scope",
        json={"scope_key": "code-1:cfg-a", "build_config": {"ns.T": {"width": 3}}},
    )
    assert same.status_code == 200, same.text
    assert same.json()["build_config"] == {"ns.T": {"width": 3}}

    unspecified = await client.put(
        f"{BUILDS}/{build_id}/scope", json={"scope_key": "code-1:cfg-a"}
    )
    assert unspecified.status_code == 200, unspecified.text
    assert unspecified.json()["build_config"] == {"ns.T": {"width": 3}}


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


# --- Rollover ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rollover_gates_over_the_new_scope_and_keeps_the_old_edges(
    client: AsyncClient,
):
    """Planned under code 1 with ``t <- u1``; re-planned under code 2 with
    ``t <- u2``. Afterwards the build gates over code 2's edges only, and
    code 1's edge is still there for any build that runs under code 1."""
    build_id = (await _build(client, "code-1:cfg"))["id"]
    await _register_task(client, build_id, "u1")
    await _register_task(client, build_id, "t", ["u1"])
    assert set(_actionable(await _frontier(client, build_id))) == {"u1"}

    # New code re-plans: registers its structure under its own scope, then
    # moves the build there.
    for task_id, deps in (("u2", None), ("t", ["u2"])):
        response = await client.post(
            f"{BUILDS}/{build_id}/tasks",
            json={**_register(task_id, deps), "scope_key": "code-2:cfg"},
        )
        assert response.status_code == 201, response.text
    moved = await client.put(
        f"{BUILDS}/{build_id}/scope", json={"scope_key": "code-2:cfg"}
    )
    assert moved.status_code == 200, moved.text

    frontier = await _frontier(client, build_id)
    assert frontier["scope_key"] == "code-2:cfg"
    # u1 no longer gates t: only u2 does. And u1 is no longer in the plan at
    # all: it was registered under code 1, whose edges the build no longer
    # reads, so counting it here would let a task run without its gates.
    # The re-plan under code 2 did not register it, so it is not code 2's.
    assert set(_actionable(frontier)) == {"u2"}, frontier
    assert frontier["status_counts"] == {"pending": 2}, frontier
    await client.post(f"{BUILDS}/{build_id}/tasks/u2/start")
    await client.post(f"{BUILDS}/{build_id}/tasks/u2/complete")
    assert "t" in _actionable(await _frontier(client, build_id))

    # Code 1's edge survives for code 1's builds.
    other = (await _build(client, "code-1:cfg"))["id"]
    await _register_task(client, other, "t")
    frontier_other = await _frontier(client, other)
    assert "t" not in _actionable(frontier_other), frontier_other
    assert "u1" in _actionable(frontier_other)


@pytest.mark.asyncio
async def test_registration_may_name_its_scope(client: AsyncClient):
    """A caller under other code than the build's current one records its
    edges under its own scope: the build's gating does not read them, and a
    build under that code does."""
    build_id = (await _build(client, "code-2:cfg"))["id"]
    await _register_task(client, build_id, "t")

    # An old worker (code 1) yields a child for t and registers it under
    # code 1's scope, in bulk and via the dependencies route.
    bulk = await client.post(
        f"{BUILDS}/{build_id}/tasks/bulk",
        json={"tasks": [_register("child")], "scope_key": "code-1:cfg"},
    )
    assert bulk.status_code == 201, bulk.text
    edges = await client.post(
        f"{BUILDS}/{build_id}/tasks/t/dependencies",
        json={
            "upstream_task_ids": ["child"],
            "is_dynamic": True,
            "scope_key": "code-1:cfg",
        },
    )
    assert edges.status_code == 200, edges.text
    assert edges.json()["added"] == 1

    # The build (code 2) is not gated by code 1's yield.
    assert "t" in _actionable(await _frontier(client, build_id))
    # A code-1 build is.
    old_code = (await _build(client, "code-1:cfg"))["id"]
    await _register_task(client, old_code, "t")
    frontier_old = await _frontier(client, old_code)
    assert "t" not in _actionable(frontier_old), frontier_old
    assert "child" in _actionable(frontier_old)


@pytest.mark.asyncio
async def test_a_registration_scope_may_be_this_builds_placeholder_only(
    client: AsyncClient,
):
    """A worker of a build nothing scoped echoes the placeholder it was
    handed; any other synthetic-shaped scope is a claim and is refused."""
    build_id = (await _build(client))["id"]
    own = await client.post(
        f"{BUILDS}/{build_id}/tasks",
        json={**_register("t"), "scope_key": f"build:{build_id}"},
    )
    assert own.status_code == 201, own.text
    other = await client.post(
        f"{BUILDS}/{build_id}/tasks",
        json={**_register("t2"), "scope_key": f"build:{uuid4()}"},
    )
    assert other.status_code == 400, other.text
    assert other.json()["detail"]["error_code"] == "synthetic_scope_claimed"
    bulk = await client.post(
        f"{BUILDS}/{build_id}/tasks/bulk",
        json={"tasks": [_register("t3")], "scope_key": f"build:{uuid4()}"},
    )
    assert bulk.status_code == 400, bulk.text


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
    for claimed in (f"build:{other}", f"build:{build_id}", f"Build:{other}"):
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

    # Only the exact placeholder shape is reserved: a code id that happens
    # to be the word ``build`` gives ``build:<16 hex>``, an ordinary claim.
    named_build = await _build(client, "build:ffffffffffffffff")
    assert named_build["scope_key"] == "build:ffffffffffffffff"


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


# --- Closure stops at completed tasks ------------------------------------


@pytest.mark.asyncio
async def test_closure_does_not_expand_from_a_completed_task(client: AsyncClient):
    """Discovery prunes at complete tasks and so does closure. A build that
    registers an already-completed task — a cached root an older client
    still sends — must not inherit that task's incomplete historical
    upstreams from the scope; an incomplete task with the same upstream
    does inherit it."""
    build_a = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_a, "u")
    await _register_task(client, build_a, "c", ["u"])
    await _register_task(client, build_a, "d", ["u"])
    # ``c`` completes with ``u`` still pending: history the scope keeps.
    await client.post(f"{BUILDS}/{build_a}/tasks/c/start")
    await client.post(f"{BUILDS}/{build_a}/tasks/c/complete")

    build_b = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_b, "c")
    frontier_b = await _frontier(client, build_b)
    assert _actionable(frontier_b) == {}, frontier_b
    assert frontier_b["status_counts"] == {"completed": 1}, frontier_b

    build_c = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_c, "d")
    frontier_c = await _frontier(client, build_c)
    assert set(_actionable(frontier_c)) == {"u"}, frontier_c
    assert frontier_c["status_counts"] == {"pending": 2}, frontier_c


@pytest.mark.asyncio
async def test_stall_time_closure_is_charged_against_the_event_quota(
    client: AsyncClient,
):
    """Every admission is a TASK_REFERENCED event, so the closure that runs
    inside the frontier read is bounded by the same 24h event quota as any
    other event path — a scope-mate's wide fan-out is not a way around it."""
    from unittest.mock import patch

    from stardag_api.limits import LimitsSettings, _entity_cache

    build_a = (await _build(client, "code-a:cfg"))["id"]
    build_b = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_a, "parent")
    await _register_task(client, build_b, "parent")
    await client.post(
        f"{BUILDS}/{build_a}/tasks/parent/start", params={"claim": "true"}
    )
    for child in ("c1", "c2", "c3"):
        await _register_task(client, build_a, child)
    response = await client.post(
        f"{BUILDS}/{build_a}/tasks/parent/dependencies",
        json={"upstream_task_ids": ["c1", "c2", "c3"], "is_dynamic": True},
    )
    assert response.status_code == 200, response.text
    await client.post(f"{BUILDS}/{build_a}/tasks/parent/suspend")

    # B stalls; re-closing would admit three children as three events.
    _entity_cache.clear()
    settings = LimitsSettings(max_events_per_workspace_24h=1)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        response = await client.get(f"{BUILDS}/{build_b}/frontier")
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error_code"] == "EVENT_CREATION_LIMIT"

    def referenced(events: list[dict]) -> int:
        return len([e for e in events if e["event_type"] == "task_referenced"])

    # The one TASK_REFERENCED on B is its own registration of ``parent``
    # (already known to the environment); nothing was admitted.
    events = (await client.get(f"{BUILDS}/{build_b}/events")).json()
    assert referenced(events) == 1, events
    _entity_cache.clear()

    # With the quota lifted the same read admits the three children.
    frontier_b = await _frontier(client, build_b)
    assert set(_actionable(frontier_b)) == {"c1", "c2", "c3"}, frontier_b
    events = (await client.get(f"{BUILDS}/{build_b}/events")).json()
    assert referenced(events) == 4, events


# --- Plan membership is per scope -----------------------------------------


@pytest.mark.asyncio
async def test_tasks_registered_under_an_earlier_scope_leave_the_plan(
    client: AsyncClient,
):
    """The incident the rule comes from. A build planned under code A had a
    worker, still on code A, yield children and register them (edges under
    A) seconds before a tick on code B re-planned the build under B. With
    membership read as "any event of this build" the children were in B's
    plan with no edges in B, looked ready, ran before their input existed and
    failed. Membership is per scope: only what was registered under the
    current scope is in the plan."""
    build_id = (await _build(client, "code-a:cfg"))["id"]
    await _register_task(client, build_id, "r")
    await client.post(f"{BUILDS}/{build_id}/tasks/r/start")
    await client.post(f"{BUILDS}/{build_id}/tasks/r/complete")
    await _register_task(client, build_id, "p", ["r"])

    # The old worker's yield, under A: children gated on a pending r2.
    bulk = await client.post(
        f"{BUILDS}/{build_id}/tasks/bulk",
        json={
            "tasks": [
                _register("r2"),
                _register("c1", ["r2"]),
                _register("c2", ["r2"]),
            ],
            "scope_key": "code-a:cfg",
        },
    )
    assert bulk.status_code == 201, bulk.text
    edges = await client.post(
        f"{BUILDS}/{build_id}/tasks/p/dependencies",
        json={
            "upstream_task_ids": ["c1", "c2"],
            "is_dynamic": True,
            "scope_key": "code-a:cfg",
        },
    )
    assert edges.status_code == 200, edges.text

    # Code B re-plans: p under B, no children yet; then the scope moves.
    response = await client.post(
        f"{BUILDS}/{build_id}/tasks",
        json={**_register("p", ["r"]), "scope_key": "code-b:cfg"},
    )
    assert response.status_code == 201, response.text
    moved = await client.put(
        f"{BUILDS}/{build_id}/scope", json={"scope_key": "code-b:cfg"}
    )
    assert moved.status_code == 200, moved.text

    frontier = await _frontier(client, build_id)
    assert frontier["scope_key"] == "code-b:cfg"
    # Not c1, c2 or r2: registered under A, edges under A, not B's plan.
    assert set(_actionable(frontier)) == {"p"}, frontier
    assert frontier["status_counts"] == {"pending": 1}, frontier

    # B's worker yields its own generation, with its gates, under B.
    bulk = await client.post(
        f"{BUILDS}/{build_id}/tasks/bulk",
        json={
            "tasks": [_register("r2"), _register("c1", ["r2"])],
            "scope_key": "code-b:cfg",
        },
    )
    assert bulk.status_code == 201, bulk.text
    edges = await client.post(
        f"{BUILDS}/{build_id}/tasks/p/dependencies",
        json={
            "upstream_task_ids": ["c1"],
            "is_dynamic": True,
            "scope_key": "code-b:cfg",
        },
    )
    assert edges.status_code == 200, edges.text
    frontier = await _frontier(client, build_id)
    # c1 is in the plan now and gated on r2; p is gated on c1; c2 stays out.
    assert set(_actionable(frontier)) == {"r2"}, frontier
    assert frontier["status_counts"] == {"pending": 3}, frontier


@pytest.mark.asyncio
async def test_registration_events_carry_the_scope_they_were_made_under(
    client: AsyncClient,
):
    """What membership reads: TASK_PENDING / TASK_REFERENCED events carry the
    registration scope, closure's admissions carry the scope it closed
    over, and lifecycle events carry none."""
    build_id = (await _build(client, "code-1:cfg"))["id"]
    await _register_task(client, build_id, "u")
    await _register_task(client, build_id, "t", ["u"])
    # An old worker under code 0 registers a task into this build.
    response = await client.post(
        f"{BUILDS}/{build_id}/tasks",
        json={**_register("w"), "scope_key": "code-0:cfg"},
    )
    assert response.status_code == 201, response.text
    await client.post(f"{BUILDS}/{build_id}/tasks/u/start")

    events = (await client.get(f"{BUILDS}/{build_id}/events")).json()
    by_type = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e["scope_key"])
    assert by_type["task_pending"].count("code-1:cfg") == 2
    assert "code-0:cfg" in by_type["task_pending"]
    assert by_type["task_started"] == [None]

    # Stall-time closure admits under the scope it closes over.
    mate = (await _build(client, "code-1:cfg"))["id"]
    await _register_task(client, mate, "v")
    edges = await client.post(
        f"{BUILDS}/{mate}/tasks/v/dependencies",
        json={"upstream_task_ids": ["t"], "is_dynamic": True},
    )
    assert edges.status_code == 200, edges.text
    # The dependencies route records the edge; the plan closes over it when
    # the build next looks stalled, which the frontier read is.
    stalled = await _frontier(client, mate)
    assert stalled["scope_key"] == "code-1:cfg"
    admitted = [
        e
        for e in (await client.get(f"{BUILDS}/{mate}/events")).json()
        if e["event_type"] == "task_referenced"
    ]
    assert admitted and all(e["scope_key"] == "code-1:cfg" for e in admitted), admitted
