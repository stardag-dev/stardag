"""A task's declared static dependencies are immutable.

A task id promises the world state its completion establishes, and that
includes the upstream set it was built from — so changing what a task
requires has to change its id. The registry cannot check that for a task it
has never seen; once one has been registered it can, and it refuses a later
declaration that contradicts the recorded one.

What is deliberately *not* here: any notion of who else is running. The
check compares two sets. It never asks whether another build holds the task,
when it started, or whether it is still alive — which is what makes this
design cheap to reason about, and is the whole difference from the
retraction-based one it replaced (see
``docs/design/immutable-dependency-declarations.md``).
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


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_changed_declaration_is_refused(client: AsyncClient):
    """The reported bug, now diagnosed rather than absorbed.

    `R` was built from `U1`; the code now says it requires `U2`. Its id did
    not move, so it still promises the state it established from `U1`. That
    promise cannot be met from a different upstream set, and the remedy is a
    version bump rather than anything the registry can do on the user's
    behalf.
    """
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])
    await client.post(f"/api/v1/builds/{first}/complete")

    second = await _new_build(client)
    await _register_task(client, second, "U2")
    response = await _register_task(client, second, "R", ["U2"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "dependency_declaration_changed"
    assert detail["task_id"] == "R"
    assert detail["declared"] == ["U2"]
    assert detail["recorded"] == ["U1"]


@pytest.mark.asyncio
async def test_a_finished_build_is_no_excuse(client: AsyncClient):
    """Deliberately stricter than the design this replaces.

    That one only refused while another build was live, which made the
    answer depend on timing. The record is what the task promised; whether
    anyone is currently acting on it is beside the point.
    """
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])
    await client.post(f"/api/v1/builds/{first}/cancel")

    second = await _new_build(client)
    assert (await _register_task(client, second, "R", ["U2"])).status_code == 409


@pytest.mark.asyncio
async def test_an_addition_is_a_change(client: AsyncClient):
    """Set equality, not "did anything get dropped". Requiring strictly more
    is still requiring something different."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    response = await _register_task(client, second, "R", ["U1", "U2"])

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["declared"] == ["U1", "U2"]


@pytest.mark.asyncio
async def test_dropping_every_dependency_is_a_change(client: AsyncClient):
    """`requires()` now returns nothing, which an empty list says exactly."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    assert (await _register_task(client, second, "R", [])).status_code == 409


@pytest.mark.asyncio
async def test_the_first_declaration_is_never_a_change(client: AsyncClient):
    """Nothing recorded is not a disagreement — it is a task the environment
    has not seen. This is the overwhelmingly common case."""
    build = await _new_build(client)
    await _register_task(client, build, "U1")
    assert (await _register_task(client, build, "R", ["U1"])).status_code == 201
    assert await _actionable(client, build) == ["U1"]


@pytest.mark.asyncio
async def test_agreeing_builds_never_conflict(client: AsyncClient):
    """Two builds of the same code must be silent. The check fires on a
    *difference*, never on concurrency."""
    first = await _new_build(client)
    await _register_task(client, first, "up")
    await _register_task(client, first, "down", ["up"])

    second = await _new_build(client)
    await _register_task(client, second, "up")
    assert (await _register_task(client, second, "down", ["up"])).status_code == 201
    assert await _actionable(client, second) == ["up"]


@pytest.mark.asyncio
async def test_a_build_never_conflicts_with_itself(client: AsyncClient):
    """A resume re-registers everything and discovery registers in chunks
    that can repeat a task. Neither is a disagreement."""
    build = await _new_build(client)
    await _register_task(client, build, "U1")
    await _register_task(client, build, "R", ["U1"])
    assert (await _register_task(client, build, "R", ["U1"])).status_code == 201


@pytest.mark.asyncio
async def test_declaration_order_does_not_matter(client: AsyncClient):
    """`requires()` returns a structure, not an ordered contract."""
    first = await _new_build(client)
    await _register_task(client, first, "A")
    await _register_task(client, first, "B")
    await _register_task(client, first, "R", ["A", "B"])

    second = await _new_build(client)
    assert (await _register_task(client, second, "R", ["B", "A"])).status_code == 201


# ---------------------------------------------------------------------------
# what is not a declaration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saying_nothing_is_not_declaring_nothing(client: AsyncClient):
    """The other half of the contract, and the reason the field is nullable.

    "I am registering this task and saying nothing about its dependencies"
    is a real intent — an out-of-band caller that does not know them, or
    discovery registering a task it pruned at. An empty list cannot carry
    both meanings, and reading silence as a declaration would refuse a
    caller that never claimed to know.
    """
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    assert (await _register_task(client, second, "R")).status_code == 201  # no key
    assert await _actionable(client, second) == ["U1"], (
        "the recorded edge must survive a registration that said nothing"
    )


@pytest.mark.asyncio
async def test_a_dynamic_edge_is_not_part_of_the_declaration(client: AsyncClient):
    """Only static edges are declared. A dynamic edge was discovered by an
    execution, and a `requires()` that does not name it is not contradicting
    it — the two are different kinds of statement about the task."""
    first = await _new_build(client)
    await _register_task(client, first, "kid")
    await _register_task(client, first, "parent", ["up"])
    await client.post(
        f"/api/v1/builds/{first}/tasks/parent/dependencies",
        json={"upstream_task_ids": ["kid"], "is_dynamic": True},
    )

    second = await _new_build(client)
    # Same static declaration; the dynamic edge is neither compared nor lost.
    assert (await _register_task(client, second, "parent", ["up"])).status_code == 201
    assert sorted(await _actionable(client, second)) == ["kid", "up"]


# ---------------------------------------------------------------------------
# the refusal is atomic, and it is the product
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_registration_writes_nothing(client: AsyncClient):
    """A build about to be refused must not leave the registry changed."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    await _register_task(client, second, "U2")
    await _register_task(client, second, "R", ["U2"])

    assert await _actionable(client, first) == ["U1"], "R was re-pointed anyway"


@pytest.mark.asyncio
async def test_bulk_registration_is_refused_whole(client: AsyncClient):
    """The batch is one transaction, so a change anywhere in it leaves none
    of it behind — including the tasks before the offending one."""
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


@pytest.mark.asyncio
async def test_the_message_says_what_changed_and_what_to_do(client: AsyncClient):
    """This message is the entire user experience of the refusal: a build
    stopped for this reason looks, to whoever triggered it, exactly like
    stardag declining to run."""
    first = await _new_build(client)
    await _register_task(client, first, "U1")
    await _register_task(client, first, "R", ["U1"])

    second = await _new_build(client)
    response = await _register_task(client, second, "R", ["U2"])

    message = response.json()["detail"]["message"]
    assert "R" in message
    assert "U1" in message and "U2" in message
    assert "no longer requires U1" in message
    assert "now requires U2" in message
    assert "__version__" in message, "the remedy has to be named, not implied"
    assert "operator" in message, "...and so does the way out when the record is wrong"


@pytest.mark.asyncio
async def test_a_leaf_cannot_quietly_gain_a_dependency(client: AsyncClient):
    """The shape an edge-only check cannot see.

    A task declared to require nothing writes no edges, so "declared []"
    and "never declared" are the same absence in `task_dependencies`.
    Without a durable marker a leaf that later gains an upstream reads as a
    first declaration and is accepted — and a leaf gaining a dependency is
    a common refactor, not a corner case.
    """
    first = await _new_build(client)
    await _register_task(client, first, "leaf", [])
    await client.post(f"/api/v1/builds/{first}/complete")

    second = await _new_build(client)
    await _register_task(client, second, "new-upstream")
    response = await _register_task(client, second, "leaf", ["new-upstream"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["recorded"] == []
    assert detail["declared"] == ["new-upstream"]
    assert "now requires new-upstream" in detail["message"]


@pytest.mark.asyncio
async def test_a_leaf_that_stays_a_leaf_is_fine(client: AsyncClient):
    """...and re-declaring the same empty set is not a change."""
    first = await _new_build(client)
    await _register_task(client, first, "still-leaf", [])
    second = await _new_build(client)
    assert (await _register_task(client, second, "still-leaf", [])).status_code == 201


@pytest.mark.asyncio
async def test_saying_nothing_never_sets_the_marker(client: AsyncClient):
    """Registering without declaring must not start the clock — otherwise
    an out-of-band caller would silently commit a task to requiring
    nothing."""
    first = await _new_build(client)
    await _register_task(client, first, "bare")  # no key at all

    second = await _new_build(client)
    assert (await _register_task(client, second, "up")).status_code == 201
    assert (await _register_task(client, second, "bare", ["up"])).status_code == 201
