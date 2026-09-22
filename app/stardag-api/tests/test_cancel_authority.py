"""Who may revoke a task, and what a terminal build releases.

``docs/design/execution-claims-and-liveness.md`` states that *authority to
revoke is build-scoped*: a task that is RUNNING, SUSPENDED or INTERRUPTED
belongs to the build whose event produced that status, and a cancel from
anyone else is refused. The cascade always enforced it
(``test_build_cleanup.test_cascade_never_cancels_another_builds_running_task``);
the per-task route did not, and a cancelled reactive build used that route
to cancel every RUNNING task in its *plan*, including ones a later build
had claimed and was executing.

**These rules are kept on purpose** (STA-81). The machinery that stopped
containers is gone — no executions listing, no conditional cancel keyed on
an executor reference, no drain in the tick — but the authority rule is not
part of it. It is what protects the claim, it costs one comparison on an
already-locked row, and removing it would re-open the incident above.

The second half of this module is the other thing STA-81 settled: a build
going terminal releases the claims it holds, by *any* route out. That used
to be true of cancel alone, with the fail path getting its release as a
side effect of the tick stopping containers.
"""

import pytest
from httpx import AsyncClient


def _register(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
        "dependency_task_ids": deps or [],
    }


async def _new_build(client: AsyncClient, **body) -> str:
    return (await client.post("/api/v1/builds", json=body)).json()["id"]


async def _start(client: AsyncClient, build_id: str, task_id: str) -> None:
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register(task_id))
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/start",
        params={"executor": "modal", "executor_ref": f"fc-{task_id}"},
    )


async def _reference(client: AsyncClient, build_id: str, task_id: str) -> None:
    """Register an existing task under another build: TASK_REFERENCED, no
    status change — exactly what plan closure does."""
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register(task_id))


async def _task_status(client: AsyncClient, task_id: str) -> str:
    return (await client.get(f"/api/v1/tasks/{task_id}")).json()["latest_status"]


async def _cancel(client: AsyncClient, build_id: str, task_id: str):
    return await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/cancel")


# ---------------------------------------------------------------------------
# authority to revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_referencing_build_cannot_cancel_a_running_task(client: AsyncClient):
    """The reported bug, at the route: build B is executing the task, build A
    merely has it in its plan, and A's cancel would both kill B's container
    and release B's claim."""
    owner = await _new_build(client)
    await _start(client, owner, "shared")

    referencer = await _new_build(client)
    await _reference(client, referencer, "shared")

    response = await _cancel(client, referencer, "shared")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "not_claim_holder"
    assert detail["latest_status_build_id"] == owner
    assert await _task_status(client, "shared") == "running"

    # The owner is refused nothing.
    assert (await _cancel(client, owner, "shared")).status_code == 200
    assert await _task_status(client, "shared") == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "status"), [("suspend", "suspended"), ("interrupt", "interrupted")]
)
async def test_ownership_covers_every_status_a_task_is_held_in(
    client: AsyncClient, route: str, status: str
):
    """Not just RUNNING. A suspension holds no claim, but it is a build's
    execution mid-flight all the same, and the cascade treats all three
    alike — the two definitions are now literally the same tuple."""
    owner = await _new_build(client)
    await _start(client, owner, "held")
    await client.post(f"/api/v1/builds/{owner}/tasks/held/{route}")
    assert await _task_status(client, "held") == status

    other = await _new_build(client)
    await _reference(client, other, "held")
    assert (await _cancel(client, other, "held")).status_code == 409
    assert await _task_status(client, "held") == status


@pytest.mark.asyncio
async def test_a_pending_task_is_cancellable_by_any_build(client: AsyncClient):
    """PENDING holds no claim and no execution, so nobody owns it. Refusing
    here would take away the one way to retire work nothing will run."""
    first = await _new_build(client)
    await client.post(f"/api/v1/builds/{first}/tasks", json=_register("idle"))

    other = await _new_build(client)
    await _reference(client, other, "idle")
    assert (await _cancel(client, other, "idle")).status_code == 200
    assert await _task_status(client, "idle") == "cancelled"


@pytest.mark.asyncio
async def test_a_terminal_task_is_cancellable_by_any_build(client: AsyncClient):
    """A cancel over a failure is bookkeeping, not revocation — and a
    COMPLETED task is sticky, so the event is recorded and the status is
    not moved. Neither case has a claim to protect."""
    owner = await _new_build(client)
    await _start(client, owner, "done")
    await client.post(f"/api/v1/builds/{owner}/tasks/done/fail")

    other = await _new_build(client)
    await _reference(client, other, "done")
    assert (await _cancel(client, other, "done")).status_code == 200
    assert await _task_status(client, "done") == "cancelled"


# ---------------------------------------------------------------------------
# what is deliberately no longer here (STA-81)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_executions_listing_is_gone(client: AsyncClient):
    """No route reconstructs "which execution is mine" from the event log.

    It existed for one caller — the tick's cancel drain — which reasoned
    about containers other processes had started. Nothing does that now: a
    worker stops itself when it is no longer wanted, and an operator stops
    one from ``stardag builds stop``, which reads the task row while the
    claims still make that reading exact. Asserted rather than merely
    deleted, because a listing keyed on the past is the thing that kept
    growing bugs, and a well-meaning restoration is the likely regression.
    """
    build = await _new_build(client)
    await _start(client, build, "running-task")
    assert (await client.get(f"/api/v1/builds/{build}/executions")).status_code == 404


@pytest.mark.asyncio
async def test_a_cancel_no_longer_takes_an_execution_condition(client: AsyncClient):
    """``if_executor`` / ``if_executor_ref`` are gone, and unknown query
    parameters are ignored rather than honoured — so an old client's
    conditional cancel becomes a plain one instead of silently doing
    nothing. That is the safe direction: the caller wanted the claim
    released, and the authority rule below still decides whether it may."""
    owner = await _new_build(client)
    await _start(client, owner, "conditioned")

    response = await client.post(
        f"/api/v1/builds/{owner}/tasks/conditioned/cancel",
        params={"if_executor": "modal", "if_executor_ref": "fc-something-else"},
    )
    assert response.status_code == 200, response.text
    assert await _task_status(client, "conditioned") == "cancelled"


# ---------------------------------------------------------------------------
# a terminal build releases the claims it holds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_build_releases_its_claims(client: AsyncClient):
    """The rule the deleted drain was quietly providing.

    Stopping each container was followed by a TASK_CANCELLED that released
    the claim, and on the fail path nothing else did it — ``/fail`` wrote a
    single BUILD_FAILED event. Deleting the drain without this would leave a
    failed build's tasks claimed until expiry, denying them to every later
    build and holding their concurrency-limit slots for that whole window.
    """
    build = await _new_build(client)
    await _start(client, build, "still-running")
    assert await _task_status(client, "still-running") == "running"

    response = await client.post(f"/api/v1/builds/{build}/fail")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert await _task_status(client, "still-running") == "cancelled"


@pytest.mark.asyncio
async def test_a_failed_build_releases_only_its_own_claims(client: AsyncClient):
    """Same scope as a cancel, and for the same reason: the server cannot
    stop a live execution, only rewrite the registry's view of one, so
    releasing a neighbour's claim would declare their live worker dead."""
    mine = await _new_build(client)
    await _start(client, mine, "mine")

    theirs = await _new_build(client)
    await _start(client, theirs, "theirs")
    await _reference(client, mine, "theirs")

    await client.post(f"/api/v1/builds/{mine}/fail")

    assert await _task_status(client, "mine") == "cancelled"
    assert await _task_status(client, "theirs") == "running", (
        "a failing build released a claim held by another build"
    )


@pytest.mark.asyncio
async def test_a_failed_build_leaves_pending_work_alone(client: AsyncClient):
    """PENDING holds no claim, and a task this build registered may be
    referenced by a live build elsewhere. ``skip-blocked`` is the operation
    for pending work whose upstreams failed."""
    build = await _new_build(client)
    await client.post(f"/api/v1/builds/{build}/tasks", json=_register("never-ran"))

    await client.post(f"/api/v1/builds/{build}/fail")

    assert await _task_status(client, "never-ran") == "pending"
