"""A claiming start delivered twice: the second one is the same execution.

The registry client retries a POST whose response never arrived, so a
claiming start that *succeeded* can be sent again. Refusing the repeat
tells a worker that somebody else is running the task -- which is a
correct reason to stand down, and it does, while holding the claim
itself. The task is then claimed and not running until the claim expires.

The identity that settles it is not the build: two attempts of one build
are legitimately distinct, and granting on the build alone would start
handing out real double-claims. It is the build *and the whole
execution*: ``(executor, executor_ref)``, which a retry repeats and a new
attempt replaces. Both halves of that pair, because a ref is
backend-specific and two backends can mint the same string without it
naming the same execution -- see
``test_the_same_ref_from_a_different_executor_is_refused``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, Response

BUILDS = "/api/v1/builds"

pytestmark = pytest.mark.asyncio


def _register(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": task_id,
        "task_data": {},
    }


async def _new_build(client: AsyncClient) -> str:
    return (await client.post(BUILDS, json={})).json()["id"]


async def _start(
    client: AsyncClient, build_id: str, task_id: str, **params: str
) -> Response:
    return await client.post(
        f"{BUILDS}/{build_id}/tasks/{task_id}/start",
        params={"claim": "true", **params},
    )


async def _claimed_task(client: AsyncClient, task_id: str) -> tuple[str, str]:
    build_id = await _new_build(client)
    await client.post(f"{BUILDS}/{build_id}/tasks", json=_register(task_id))
    first = await _start(
        client, build_id, task_id, executor="modal", executor_ref="fc-1"
    )
    assert first.status_code == 200, first.text
    return build_id, task_id


async def test_the_same_execution_asking_again_is_granted(client: AsyncClient):
    """The retry: same build, same executor_ref."""
    build_id, task_id = await _claimed_task(client, "retried-claim")

    again = await _start(
        client, build_id, task_id, executor="modal", executor_ref="fc-1"
    )

    assert again.status_code == 200, (
        "the execution holding the claim was refused its own claim, so a "
        f"retried start reads as a lost race: {again.text}"
    )


async def test_a_second_attempt_of_the_same_build_is_still_refused(
    client: AsyncClient,
):
    """The case that makes the build id insufficient on its own.

    A retried task gets a new container, so the same build starting the
    task again is a *different* execution — and the live claim must still
    deny it. Granting on the build alone would turn the claim into no
    claim at all for the commonest double-run there is.
    """
    build_id, task_id = await _claimed_task(client, "second-attempt")

    again = await _start(
        client, build_id, task_id, executor="modal", executor_ref="fc-2"
    )

    assert again.status_code == 409, (
        "a second execution of the same build was granted a claim the "
        f"first still holds: {again.text}"
    )
    assert again.json()["detail"]["error_code"] == "task_already_running"
    # And it is told which execution holds it, so it can re-attach.
    assert again.json()["detail"]["executor_ref"] == "fc-1"


async def test_the_same_ref_from_a_different_executor_is_refused(
    client: AsyncClient,
):
    """A ref only means something alongside the backend that minted it.

    Refs are backend-specific — a Modal function call id, a pod name, a
    local run counter — so two backends can produce the same string
    without it naming the same execution. Matching on the ref alone would
    read such a start as the holder and grant it a second claim, which is
    the one thing the claim exists to prevent. The rest of the system
    already reads these two columns as a pair: ``DetachedHandle`` records
    both so a ref is only handed back to the backend that created it.
    """
    build_id, task_id = await _claimed_task(client, "same-ref-other-backend")

    again = await _start(
        client, build_id, task_id, executor="local", executor_ref="fc-1"
    )

    assert again.status_code == 409, (
        "a start from a different backend was granted the claim because it "
        f"reused the ref string: {again.text}"
    )
    assert again.json()["detail"]["error_code"] == "task_already_running"
    assert again.json()["detail"]["executor"] == "modal"


async def test_a_start_without_an_executor_ref_is_refused(client: AsyncClient):
    """With nothing to compare, a retry and a second attempt are the same
    request, and the safe answer to "I cannot tell" is the one that never
    double-claims."""
    build_id, task_id = await _claimed_task(client, "anonymous-start")

    again = await _start(client, build_id, task_id)

    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error_code"] == "task_already_running"


async def test_another_build_is_still_refused(client: AsyncClient):
    """The property the claim exists for, unchanged -- including against a
    second build that happens to report the same executor_ref."""
    _, task_id = await _claimed_task(client, "shared-claim")
    other_build = await _new_build(client)

    denied = await _start(
        client, other_build, task_id, executor="modal", executor_ref="fc-1"
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["error_code"] == "task_already_running"
