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

from stardag.utils.testing.helper_tasks import SyncOnlyTask

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

        # The task fails and is retried, which resets it and clears the
        # identity with it -- through the double's own retry path, so
        # the reset is modelled rather than arranged here. RUNNING is
        # deliberately not retryable (it holds a live claim), so the
        # failure is part of the sequence, not scaffolding.
        registry.statuses[tid] = "failed"
        await registry.task_retry_aio(uuid4(), root)
        assert registry.execution_ids.get(tid) is None, (
            "the double kept an identity across a retry, where the "
            "server's TASK_RETRIED fold clears it"
        )
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


class TestTheIdentityReachesTheSpawn:
    async def test_the_spawn_is_told_which_execution_it_is(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The claim's identity has to reach the container, or the worker
        cannot name its own execution — and then neither of the two rules
        it exists for applies to the worker's own reports."""
        (root,) = _chain("spawn-identity-root")
        registry, executor = _setup([root], auto_complete=False)

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        assert executor.spawned_execution_ids, "nothing was spawned"
        spawned = executor.spawned_execution_ids[0]
        assert spawned is not None, "the spawn was given no identity"
        assert str(spawned) == registry.sent_execution_ids[str(root.id)][0], (
            "the spawn and the claim named different executions"
        )

    async def test_the_post_spawn_start_repeats_the_claims_identity(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Otherwise the registry would read the tick's own ref-recording
        start as a *different* execution and refuse it."""
        (root,) = _chain("post-spawn-root")
        registry, executor = _setup([root], auto_complete=False)

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        # First and last, not first and second: the fake's limit-slot
        # acquire goes through the same start path with no identity of its
        # own, exactly as the server's enforced start does.
        sent = registry.sent_execution_ids[str(root.id)]
        assert len(sent) >= 2, "the post-spawn start recorded no identity"
        assert sent[-1] == sent[0] and sent[0] is not None, (
            f"the post-spawn start named a different execution: {sent}"
        )


class TestALostRaceDuringTheSpawn:
    """The post-spawn start's 409, which must not take the tick with it.

    The window is real and short: the claim is granted, the spawn is in
    flight, and meanwhile the claim lapses or a cascading cancel releases
    it and somebody else takes the task. The registry is right to refuse
    the ref — recording it would stamp this execution over the live
    holder's — but the refusal arrives inside a ``TaskGroup``, where an
    escaping error cancels every sibling spawn and kills the pass. Those
    siblings would be left claimed and never spawned until their claims
    expire, which is a far worse outcome than the one task that was
    genuinely lost.
    """

    def _taken_over_during_spawn(self, registry, executor, victim_id: str):
        """Make ``victim_id``'s task change hands while its spawn runs."""
        original = executor.submit_detached

        async def submit_detached(task, *, execution_id=None):
            handle = await original(task, execution_id=execution_id)
            if str(task.id) == victim_id:
                # Somebody else claimed it while we were spawning.
                registry.execution_ids[victim_id] = str(uuid4())
            return handle

        executor.submit_detached = submit_detached

    async def test_the_siblings_still_spawn_and_the_tick_survives(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        first = SyncOnlyTask(name="lost-one", deps=())
        second = SyncOnlyTask(name="kept-one", deps=())
        root = SyncOnlyTask(name="lost-race-root", deps=(first, second))
        registry, executor = _setup([first, second, root], auto_complete=False)
        self._taken_over_during_spawn(registry, executor, str(first.id))

        # No exception: the 409 is handled, not propagated.
        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        spawned = {str(t) for t in executor.spawned}
        assert str(second.id) in spawned, (
            "a sibling spawn was cancelled by the lost task's 409"
        )

    async def test_the_orphaned_container_is_stopped_while_we_hold_it(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The half of the fix that is easy to lose in a re-implementation.

        Catching the 409 is obvious. Remembering that the container is
        *already running with a ref nobody recorded* — so nothing else can
        ever address it — is not. The handle is in hand exactly here and
        nowhere later, so this is the only place it can be stopped. Its own
        cooperative checkpoint would get it eventually; this is faster and
        costs one call we can already make.
        """
        (root,) = _chain("orphan-root")
        registry, executor = _setup([root], auto_complete=False)
        self._taken_over_during_spawn(registry, executor, str(root.id))

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        assert executor.cancelled_refs, (
            "the container we spawned and then lost was left running with "
            "a reference nothing recorded"
        )
