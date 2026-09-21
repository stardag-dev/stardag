"""A retried claim is the same attempt asking again, against the real arbiter.

The registry client retries a POST whose response never arrived, so a
claiming start that *succeeded* can be delivered twice. Refused, the
second delivery tells the worker that somebody else holds the task — a
correct reason to stand down, and it does, while itself holding the
claim, leaving the task claimed and not running until the claim expires.

The claim is taken before the spawn, so there is no executor ref to
identify the attempt by and never was; ``execution_id`` is the identity
that exists anyway. What makes this worth a live scenario rather than
only a unit test is the arbiter: a real Postgres row held ``FOR UPDATE``
by the deployed API, reached over the network, rather than a double
written from the same understanding as the code it stands in for.

Driven directly rather than through a spawned build. Reproducing a lost
response against a live API is not something a test can arrange, and the
delivery the client would repeat is this exact call — so a worker would
add a second thing that can fail without adding anything to the
property under test.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(300),
]


def test_a_retried_claim_is_granted_to_the_attempt_that_won_it() -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import get_range

    salt = uuid.uuid4().hex
    task = get_range(limit=3, salt=salt)
    registry = registry_provider.get()

    build_id = registry.build_start([task], description="STA-50 retried claim")
    registry.task_register(build_id, task)
    execution_id = uuid.uuid4()

    async def _claim(eid: uuid.UUID):
        return await registry.task_start_claim_aio(build_id, task, execution_id=eid)

    first = asyncio.run(_claim(execution_id))
    assert first.started, f"the first claim was denied: {first}"
    assert first.execution_id == str(execution_id), (
        "The registry did not echo the claim identity, so it predates the "
        f"field and this scenario proves nothing: {first}"
    )

    retried = asyncio.run(_claim(execution_id))
    assert retried.started, (
        "A retried claim was refused, so a worker holding the claim would "
        f"stand down from its own task: {retried}"
    )

    second_attempt = asyncio.run(_claim(uuid.uuid4()))
    assert not second_attempt.started, (
        "A genuine second attempt was granted while the claim was live, so "
        f"the identity has cost the exactly-once guarantee: {second_attempt}"
    )
    assert second_attempt.denied_reason == "already_running"
    assert second_attempt.execution_id == str(execution_id), (
        "The denial did not name the claim that holds the task."
    )
