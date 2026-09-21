"""One execution, one identity, minted before the claim.

The engines claim a task *before* spawning it — the claim and any
concurrency-limit slots have to be acquired in one transaction, so a
denied task never occupies a worker — which means the executor reference
does not exist yet. ``execution_id`` is the identity that does, and it is
what the registry uses to tell a retried claim from a second attempt, and
a worker's own start from a superseded execution's.

What these tests pin is the SDK's half: that one execution produces one
id, that it reaches everything that speaks about that execution
(including the container), and that a new attempt gets a new one. The
registry-side rules are in ``app/stardag-api``'s
``test_execution_identity``; the end-to-end shape is the registry-live
tier.
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


class TestTheReactiveTickMintsOne:
    async def test_the_claim_the_ref_and_the_spawn_share_one_id(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Three calls describe one execution, so they carry one id.

        The tick claims, spawns, then starts again to record the ref, and
        the worker inside the container starts a third time. Before the
        identity these were three unrelated starts and the registry could
        only guess which execution each was about.
        """
        (root,) = _chain("mint-root")
        registry, executor = _setup([root], auto_complete=False)

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        sent = registry.sent_execution_ids[str(root.id)]
        assert len(sent) == 2, f"expected a claim and a ref-recording start: {sent}"
        assert sent[0] is not None, "the claim went out with no identity"
        assert len(set(sent)) == 1, (
            f"the claim and its ref-recording start named different executions: {sent}"
        )
        assert registry.execution_ids[str(root.id)] == sent[0]

    async def test_the_identity_reaches_the_container(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The worker reports its own start, and that start is the one the
        supersession rule judges — so an identity that stopped at the
        registry would leave the hottest report unable to name itself."""
        (root,) = _chain("spawn-root")
        registry, executor = _setup([root], auto_complete=False)

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        spawned = executor.spawned_execution_ids
        assert spawned and spawned[0] is not None, "the spawn carried no identity"
        assert str(spawned[0]) == registry.sent_execution_ids[str(root.id)][0]

    async def test_a_second_attempt_mints_a_new_one(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The property the identity must not cost. A retry is a different
        execution and has to be refused if the first still holds the
        claim, so reusing the id across attempts would turn the
        idempotent-retry grant into a licence to double-run."""
        (root,) = _chain("retry-root")
        registry, executor = _setup([root], auto_complete=False)
        config = TickConfig(**{**FAST_TICK.__dict__})

        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=config
        )
        first = registry.sent_execution_ids[str(root.id)][0]

        # Fail and reset the task, then let a second pass spawn it again.
        tid = str(root.id)
        registry.statuses[tid] = "pending"
        registry.execution_ids[tid] = None
        await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=config
        )

        sent = registry.sent_execution_ids[tid]
        assert sent[-1] != first, (
            "a second attempt reused the first execution's identity, so the "
            "registry would read it as a retry and grant the claim twice"
        )

    async def test_losing_the_task_during_the_spawn_does_not_kill_the_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The post-spawn start can be refused, and must not propagate.

        Between the claim and the start that records the ref, the claim
        can lapse or be cancel-released and the task taken over. The
        registry then refuses the ref — correctly, since recording it
        would stamp our execution over the live holder's.

        That refusal arrives inside a ``TaskGroup``, so letting it escape
        cancels every sibling spawn in the pass and kills the tick,
        leaving those siblings claimed and never spawned until their
        claims expire. And the container is already running with its ref
        unrecorded, so nothing else can find it: the pass has to stop it
        while it still holds the handle.
        """
        (root,) = _chain("spawn-race-root")
        registry, executor = _setup([root], auto_complete=False)
        tid = str(root.id)
        stolen = uuid4()

        original = executor.submit_detached

        async def steal_during_spawn(task, *, execution_id=None):
            handle = await original(task, execution_id=execution_id)
            # Somebody else takes the task over while we are spawning.
            registry.execution_ids[tid] = str(stolen)
            return handle

        executor.submit_detached = steal_during_spawn  # type: ignore[assignment]

        summary = await run_tick_aio(
            uuid4(), registry=registry, task_executor=executor, config=FAST_TICK
        )

        assert summary.outcome != "error", (
            f"the refused ref killed the tick: {summary.error_message}"
        )
        assert summary.spawned == 0, "a lost task must not count as spawned"
        assert summary.claim_denied == 1
        assert executor.cancelled_refs, (
            "the orphaned container was left running with no recorded ref, "
            "so nothing can ever address it"
        )


class TestTheRegistryRulesTheFakeModels:
    async def test_a_retried_claim_is_granted(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The double models the server rule, so a test can falsify it.

        The old fake could not: it keyed on status alone, which is the
        same misconception the endpoint had, and a double that shares the
        code's misconception cannot catch it.
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
        assert again.started, "the same execution asking twice was refused"
        assert not other.started, "a second attempt was granted the live claim"
        assert other.denied_reason == "already_running"

    async def test_a_superseded_start_is_refused(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A worker whose claim was taken over must not take it back."""
        from stardag.exceptions import APIError

        (root,) = _chain("superseded-root")
        registry, _ = _setup([root], auto_complete=False)
        build_a, build_b = uuid4(), uuid4()
        a_execution, b_execution = uuid4(), uuid4()

        await registry.task_start_aio(build_a, root, execution_id=a_execution)
        # A's claim lapses and B takes the task over. A takeover is always
        # a *claiming* start -- that is the healing mechanism, and it is
        # deliberately exempt from the supersession rule, which exists to
        # judge a worker's own non-claiming report.
        registry.statuses[str(root.id)] = "pending"
        granted = await registry.task_start_claim_aio(
            build_b, root, execution_id=b_execution
        )
        assert granted.started, "the takeover was refused, so the setup is wrong"

        try:
            await registry.task_start_aio(build_a, root, execution_id=a_execution)
        except APIError as e:
            assert e.status_code == 409
            assert (e.payload or {}).get("error_code") == "execution_superseded"
        else:
            raise AssertionError(
                "a start from the superseded execution was accepted, so the "
                "live holder's claim was evicted"
            )

    async def test_a_granted_claim_inherits_no_identity(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A claim that brings no identity leaves none behind, unlike an
        ordinary start, which preserves. Mirroring both is what stops a
        replacement inheriting the identity of what it replaced."""
        (root,) = _chain("claim-clears-root")
        registry, _ = _setup([root], auto_complete=False)
        build_id = uuid4()
        tid = str(root.id)

        await registry.task_start_aio(build_id, root, execution_id=uuid4())
        registry.statuses[tid] = "pending"
        granted = await registry.task_start_claim_aio(build_id, root)

        assert granted.started
        assert registry.execution_ids[tid] is None, (
            "the replacement claim inherited the identity it replaced"
        )


class TestTheWorkerReadsItsOwn:
    def test_the_reporter_takes_the_identity_from_its_environment(self):
        """The orchestrator forwards it per call; the worker names it on
        every report it makes about itself."""
        from stardag.integration.modal._metadata import STARDAG_EXECUTION_ID_ENV
        from stardag.integration.modal._runner import _parsed_execution_id

        execution_id = uuid4()

        assert _parsed_execution_id(str(execution_id)) == execution_id
        assert STARDAG_EXECUTION_ID_ENV == "STARDAG_EXECUTION_ID"

    def test_an_unusable_identity_is_dropped_rather_than_raised_on(self):
        """It decides whether a report can name its execution, and no
        worker should fail to report its own start over it. Dropping it
        costs only the identity-based rules, which is how a worker behaved
        before they existed."""
        from stardag.integration.modal._runner import _parsed_execution_id

        assert _parsed_execution_id("not-a-uuid") is None
        assert _parsed_execution_id("") is None
        assert _parsed_execution_id(None) is None
