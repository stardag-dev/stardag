"""The tick mints a claim identity, and re-sends it on a retry.

The reactive engine claims a task *before* spawning it — the claim and
any concurrency-limit slots have to be acquired in one transaction, so a
denied task never occupies a worker — which means the executor reference
does not exist yet. ``execution_id`` is the identity that does, and it
is what lets the registry tell a retried claiming start from a genuine
second attempt.

What these tests pin is the SDK's half: that a claim carries one, that a
new attempt gets a new one, and that the double models the server rule
rather than accepting the parameter and dropping it. The server-side
rule is in ``app/stardag-api``'s ``test_execution_identity``; the
end-to-end shape is the registry-live tier.
"""

from __future__ import annotations

import typing
from uuid import uuid4

from stardag.build import TickConfig, run_tick_aio
from stardag.target import InMemoryFileTarget

from tests.test_build.reactive_fakes import (
    FAST_TICK,
    _chain,
    _setup,
)


class TestTheTickMintsOne:
    async def test_the_claim_carries_an_identity(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without one, a re-delivered claim is indistinguishable from a
        second attempt and is refused — so the worker that won stands
        down and the task sits claimed and not running."""
        (root,) = _chain("mint-root")
        registry, executor = _setup([root], auto_complete=False)

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        sent = registry.sent_execution_ids[str(root.id)]
        assert sent and sent[0] is not None, "the claim went out with no identity"
        assert registry.execution_ids[str(root.id)] == sent[0], (
            "the granted claim did not record the identity it was given"
        )

    async def test_a_second_attempt_mints_a_new_one(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The property the identity must not cost. A second attempt has
        to be refused while the first still holds the claim, so reusing
        the id across attempts would turn the idempotent-retry grant
        into a licence to double-run."""
        (root,) = _chain("retry-root")
        registry, executor = _setup([root], auto_complete=False)
        config = TickConfig(**{**FAST_TICK.__dict__})
        tid = str(root.id)

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=config
        )
        first = registry.sent_execution_ids[tid][0]

        # The task is reset and a later pass claims it again.
        registry.statuses[tid] = "pending"
        registry.execution_ids[tid] = None
        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=config
        )

        assert registry.sent_execution_ids[tid][-1] != first, (
            "a second attempt reused the first claim's identity, so the "
            "registry would read it as a retry and grant the claim twice"
        )


class TestTheRuleTheFakeModels:
    async def test_a_retried_claim_is_granted(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The double models the server rule, so a test can falsify it.

        The old fake keyed on status alone — the same misconception the
        endpoint had — and a double that shares the code's misconception
        cannot catch it.
        """
        (root,) = _chain("retry-claim-root")
        registry, _ = _setup([root], auto_complete=False)
        build_id = uuid4()
        execution_id = uuid4()

        first = await registry.task_start_claim_aio(
            build_id, root, execution_id=execution_id
        )
        again = await registry.task_start_claim_aio(
            build_id, root, execution_id=execution_id
        )
        other = await registry.task_start_claim_aio(
            build_id, root, execution_id=uuid4()
        )

        assert first.started
        assert first.execution_id == str(execution_id), "the grant did not echo"
        assert again.started, "the same attempt asking twice was refused"
        assert not other.started, "a second attempt was granted the live claim"
        assert other.denied_reason == "already_running"
        assert other.execution_id == str(execution_id), (
            "the denial did not name the claim that holds the task"
        )

    async def test_another_build_is_refused_even_with_the_same_id(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Build ownership is tested first, so the identity cannot be
        used by a neighbour to take a live claim."""
        (root,) = _chain("cross-build-root")
        registry, _ = _setup([root], auto_complete=False)
        execution_id = uuid4()

        won = await registry.task_start_claim_aio(
            uuid4(), root, execution_id=execution_id
        )
        denied = await registry.task_start_claim_aio(
            uuid4(), root, execution_id=execution_id
        )

        assert won.started
        assert not denied.started
        assert denied.denied_reason == "already_running"
