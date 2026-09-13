"""A task's declared static upstreams are authoritative — unless two live
builds disagree.

Static and dynamic edges go stale for different reasons, and the difference
is what decides the rule for each. A dynamic edge is *discovered* by one
execution attempt, and only one build can execute a task at a time, so
divergence there is sequential and retraction is enough. A static edge is
*declared*, in full, by every build that registers the task — and `U1` and
`U2` are different tasks with no claim between them, so two live builds
really can be materialising one downstream over two different upstream DAGs
at the same moment. Both declarations are legitimate; running both is waste
nobody asked for.

So the declaration wins when nobody else is on the task, and is refused when
somebody is.
"""

import pytest
from httpx import AsyncClient


def _register(task_id: str, deps: list[str] | None = None) -> dict:
    payload = {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
    }
    if deps is not None:
        payload["dependency_task_ids"] = deps
    return payload


async def _new_build(client: AsyncClient) -> str:
    return (await client.post("/api/v1/builds", json={})).json()["id"]


async def _register_task(
    client: AsyncClient, build_id: str, task_id: str, deps: list[str] | None = None
):
    return await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register(task_id, deps)
    )


async def _actionable(client: AsyncClient, build_id: str) -> list[str]:
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    return [ref["task_id"] for ref in frontier["actionable"]]


async def _blockers(client: AsyncClient, build_id: str) -> list[tuple[str, str]]:
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    return [
        (b["task_id"], b["blocking_task_id"]) for b in frontier["blocked_by_external"]
    ]


# ---------------------------------------------------------------------------
# the declaration is authoritative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_re_pointed_task_stops_being_gated_on_its_old_upstream(
    client: AsyncClient,
):
    """The reported bug, end to end.

    `R` is re-pointed from `U1` to `U2` — the ordinary way a pipeline
    changes, since `R`'s own promise has not changed and so neither has its
    id. Without this, `R` stayed gated on a `U1` that nothing would ever
    produce again, and the only escape was to bump `R`'s own version.
    """
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])
    await client.post(f"/api/v1/builds/{first}/tasks/U1/start")
    await client.post(f"/api/v1/builds/{first}/cancel", params={"cascade": "true"})

    second = await _new_build(client)
    await _register_task(client, second, "U2")
    await _register_task(client, second, "R", ["U2"])
    await client.post(f"/api/v1/builds/{second}/tasks/U2/start")
    await client.post(f"/api/v1/builds/{second}/tasks/U2/complete")

    assert await _actionable(client, second) == ["R"], (
        "R is still gated on the upstream its requires() no longer returns"
    )
    assert await _blockers(client, second) == []


@pytest.mark.asyncio
async def test_an_empty_declaration_drops_every_static_edge(client: AsyncClient):
    """A task whose ``requires()`` now returns nothing declares exactly
    that, and an empty list has to be able to say it."""
    first = await _new_build(client)
    await _register_task(client, first, "up")
    await _register_task(client, first, "down", ["up"])
    await client.post(f"/api/v1/builds/{first}/complete")

    second = await _new_build(client)
    await _register_task(client, second, "down", [])

    assert await _actionable(client, second) == ["down"]


@pytest.mark.asyncio
async def test_an_omitted_declaration_drops_nothing(client: AsyncClient):
    """The other half of the same contract. "I am registering this task and
    saying nothing about its dependencies" is a real intent — an out-of-band
    caller that does not know them — and an empty list cannot carry both
    meanings. Dropping edges on it would be silent data loss for a caller
    that never claimed to know."""
    first = await _new_build(client)
    await _register_task(client, first, "up")
    await _register_task(client, first, "down", ["up"])
    await client.post(f"/api/v1/builds/{first}/complete")

    second = await _new_build(client)
    await _register_task(client, second, "down")  # no key at all

    assert await _actionable(client, second) == ["up"], (
        "the edge survived, so `down` is still gated on `up`"
    )


@pytest.mark.asyncio
async def test_a_static_declaration_leaves_dynamic_edges_alone(client: AsyncClient):
    """Only what was declared is superseded. A dynamic edge was never part
    of any declaration, so a declaration cannot drop it — it goes stale by
    its own rule, when the attempt that yielded it is abandoned."""
    first = await _new_build(client)
    await _register_task(client, first, "kid")
    await _register_task(client, first, "parent", ["gone"])
    await client.post(
        f"/api/v1/builds/{first}/tasks/parent/dependencies",
        json={"upstream_task_ids": ["kid"], "is_dynamic": True},
    )
    await client.post(f"/api/v1/builds/{first}/complete")

    second = await _new_build(client)
    await _register_task(client, second, "parent", [])

    # "gone" was declared and is dropped; "kid" was yielded and remains.
    assert await _actionable(client, second) == ["kid"]


# ---------------------------------------------------------------------------
# ...unless somebody else is building it that way
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_build_on_the_task_makes_it_a_conflict(client: AsyncClient):
    """The case the whole check exists for: two builds materialising one
    downstream over different upstream DAGs at the same time."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    await _register_task(client, second, "U2")
    response = await _register_task(client, second, "R", ["U2"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "dependency_declaration_conflict"
    assert detail["task_id"] == "R"
    assert detail["declared"] == ["U2"]
    assert detail["recorded"] == ["U1"]
    assert detail["conflicting_build_ids"] == [first]
    # The message is the product: it has to name the task, what changed, who
    # is in the way, what to do, and what that costs — the last one being
    # the part most easily left out and the most expensive to omit, since a
    # reader acting on "cancel it" needs to know the other build's remaining
    # work goes with it.
    message = detail["message"]
    assert "U1" in message and "U2" in message
    assert str(first) in message
    assert f"stardag builds cancel {first} --cascade" in message
    assert "let it finish" in message
    assert "re-triggering on the new code" in message


@pytest.mark.asyncio
async def test_the_message_agrees_with_itself_about_number(client: AsyncClient):
    """Two builds in the way read as two, not as "build(s)". Small, and the
    reason to bother is that this message is the entire user experience of
    the refusal."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])
    second = await _new_build(client)
    await _register_task(client, second, "R", ["U1"])  # agrees; just holds it

    third = await _new_build(client)
    await _register_task(client, third, "U2")
    response = await _register_task(client, third, "R", ["U2"])

    message = response.json()["detail"]["message"]
    assert "builds" in message and "hold that task" in message
    assert "those builds have to be out of the way" in message
    assert str(first) in message and str(second) in message


@pytest.mark.asyncio
async def test_a_refused_registration_writes_nothing(client: AsyncClient):
    """A build that is about to be refused must not leave the registry
    changed — no superseded edges on behalf of a build that never ran, and
    no half-registered plan."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    await _register_task(client, second, "U2")
    await _register_task(client, second, "R", ["U2"])

    # The first build is untouched: R is still gated on U1, not on U2.
    assert await _actionable(client, first) == ["U1"]
    single = (await client.get("/api/v1/tasks/R")).json()
    assert single is not None


@pytest.mark.asyncio
async def test_a_finished_build_is_not_a_conflict(client: AsyncClient):
    """Only a *live* build can be harmed by re-pointing the task. A build
    that is done has nothing left to schedule."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])
    await client.post(f"/api/v1/builds/{first}/complete")

    second = await _new_build(client)
    await _register_task(client, second, "U2")
    assert (await _register_task(client, second, "R", ["U2"])).status_code == 201


@pytest.mark.asyncio
async def test_a_completed_task_is_never_a_conflict(client: AsyncClient):
    """Nobody is going to build it again, so two declarations about it
    cannot both be materialised — there is nothing to refuse.

    Not a corner case: discovery prunes *below* a complete task but still
    registers it, so every build sends a declaration for every complete task
    in its closure. Those are exactly the ones most likely to have been
    recorded long ago under older code, and refusing over them would block
    triggers on a disagreement about work that is already done.
    """
    first = await _new_build(client)
    await _register_task(client, first, "old-dep")
    await _register_task(client, first, "done", ["old-dep"])
    await client.post(f"/api/v1/builds/{first}/tasks/done/start")
    await client.post(f"/api/v1/builds/{first}/tasks/done/complete")
    # `first` stays RUNNING and still holds `done`.

    second = await _new_build(client)
    await _register_task(client, second, "new-dep")
    response = await _register_task(client, second, "done", ["new-dep"])

    assert response.status_code == 201, response.text
    # ...and the record follows the code that last described it.
    assert "old-dep" not in await _actionable(client, second)


@pytest.mark.asyncio
async def test_agreeing_builds_never_conflict(client: AsyncClient):
    """The normal case — two builds of the same code — must be silent. The
    check fires on a *difference*, not on concurrency."""
    first = await _new_build(client)
    await _register_task(client, first, "up")
    await _register_task(client, first, "down", ["up"])

    second = await _new_build(client)
    await _register_task(client, second, "up")
    assert (await _register_task(client, second, "down", ["up"])).status_code == 201
    assert await _actionable(client, second) == ["up"]


@pytest.mark.asyncio
async def test_a_build_never_conflicts_with_itself(client: AsyncClient):
    """A resume re-registers everything, and discovery registers in chunks
    that can repeat a task. Neither is a disagreement."""
    build = await _new_build(client)
    await _register_task(client, build, "U1")
    await _register_task(client, build, "U2")
    await _register_task(client, build, "R", ["U1"])

    assert (await _register_task(client, build, "R", ["U2"])).status_code == 201
    assert sorted(await _actionable(client, build)) == ["U1", "U2"]


@pytest.mark.asyncio
async def test_bulk_registration_is_refused_whole(client: AsyncClient):
    """The batch is one transaction, so a conflict anywhere in it leaves
    none of it behind — including the tasks before the offending one."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    response = await client.post(
        f"/api/v1/builds/{second}/tasks/bulk",
        json={
            "tasks": [
                _register("U2"),
                _register("innocent"),
                _register("R", ["U2"]),
            ]
        },
    )
    assert response.status_code == 409, response.text

    frontier = (await client.get(f"/api/v1/builds/{second}/frontier")).json()
    assert frontier["status_counts"] == {}, (
        f"a refused batch left tasks behind: {frontier['status_counts']}"
    )
