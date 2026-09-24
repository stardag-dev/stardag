"""The reactive tick on the v2 registry (unit tier, against the in-memory
registry): discovery jobs, claims and spawns, the end of a build, rollover,
the lease and the exit handshake."""

from __future__ import annotations

import asyncio
import typing
from uuid import UUID

import pytest

from stardag.exceptions import NotFoundError

from stardag import BaseTask, auto_namespace
from stardag.build import TickConfig, run_tick_aio
from stardag.build._reactive import roll_over_aio
from stardag.build._registration import (
    new_id,
    register_members_aio,
    register_plan_aio,
    walk_aio,
)
from stardag.registry import BuildFrontier
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import refuse
from stardag.utils.testing.dynamic_deps_dag import DynamicDepsTask
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.fakes import FakeDetachedExecutor

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]

FAST = TickConfig(linger_seconds=0.5, poll_interval_seconds=0.01)


def _chain() -> tuple[SyncOnlyTask, SyncOnlyTask, SyncOnlyTask]:
    leaf = SyncOnlyTask(name=f"leaf-{new_id()}")
    mid = SyncOnlyTask(name="mid", deps=(leaf,))
    root = SyncOnlyTask(name="root", deps=(mid,))
    return leaf, mid, root


async def _plan(
    registry: InMemoryRegistry,
    roots: list[BaseTask],
    *,
    app: str = "app",
    deployment_id: UUID | None = None,
    settings: dict[str, str] | None = None,
    expand: bool = True,
    reactive: bool = True,
) -> tuple[UUID, UUID]:
    """A reactive build planned the way the bootstrap plans it. Returns
    ``(build_id, deployment_id)``."""
    if deployment_id is None:
        deployment_id = registry.add_deployment(app_name=app)
    build_id = registry.build_create(root_task_ids=[str(r.id) for r in roots]).id
    walk = await walk_aio(roots)
    if expand:
        await register_plan_aio(
            registry,
            build_id,
            walk,
            deployment_id=deployment_id,
            settings=settings or {},
        )
    else:
        registry.plan_create(
            build_id,
            plan_id=new_id(),
            deployment_id=deployment_id,
            settings=settings or {},
            roots=walk.root_items(),
        )
    if reactive:
        registry.build_set_reactive_meta(build_id, app_name=app)
    return build_id, deployment_id


async def _tick(registry, build_id, executor, config: TickConfig = FAST, **kwargs):
    summary = await run_tick_aio(
        build_id, registry=registry, task_executor=executor, config=config, **kwargs
    )
    if isinstance(executor, FakeDetachedExecutor):
        await executor.drain()
    return summary


class TestDriving:
    async def test_a_chain_is_driven_to_completion(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        build_id, _ = await _plan(registry, [root])
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _tick(registry, build_id, executor)
        assert (summary.outcome, summary.terminal_status) == ("terminal", "completed")
        assert summary.spawned == 3
        assert registry.builds[build_id].status == "completed"
        assert root.complete()

    async def test_the_claim_names_its_execution_and_the_spawn_carries_it(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"one-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        executor = FakeDetachedExecutor(
            registry=registry, workers=True, timeout_seconds=120
        )
        config = TickConfig(
            linger_seconds=0.5,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["pool"],
        )
        await _tick(registry, build_id, executor, config)
        claim = next(
            c for c in registry.calls_to("member_start", task_id=task.id) if c["claim"]
        )
        assert claim["claim_ttl_seconds"] == 120 + 900
        assert claim["limit_keys"] == ["pool"]
        ((_, spawned_execution, _),) = executor.spawns
        assert spawned_execution == claim["execution_id"]

    async def test_settings_are_applied_in_the_pass_and_reach_the_spawn(
        self, default_in_memory_fs_target: Target
    ):
        from stardag.build import get_current_build_context

        seen: list[dict[str, str]] = []

        class Recording(FakeDetachedExecutor):
            async def submit_detached(self, task, *, execution_id):
                import os

                context = get_current_build_context()
                assert context is not None
                seen.append(dict(context.settings))
                assert os.environ.get("THREADS") == "4"
                return await super().submit_detached(task, execution_id=execution_id)

        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"set-{new_id()}")
        build_id, _ = await _plan(registry, [task], settings={"THREADS": "4"})
        await _tick(registry, build_id, Recording(registry=registry, workers=True))
        assert seen == [{"THREADS": "4"}]

    async def test_a_dynamic_yield_suspends_and_the_parent_reruns(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        child = DynamicDepsTask(value=f"child-{new_id()}")
        parent = DynamicDepsTask(value=f"parent-{new_id()}", dynamic_deps=(child,))
        build_id, _ = await _plan(registry, [parent])
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry, workers=True)
        )
        assert summary.terminal_status == "completed"
        (yield_call,) = registry.calls_to("member_yield", task_id=parent.id)
        assert yield_call["suspend"] is True
        # The parent ran twice: once to its yield, once to its end.
        assert summary.spawned == 3


class TestDiscoveryJobs:
    async def test_unexpanded_roots_are_expanded_and_the_plan_sealed(
        self, default_in_memory_fs_target: Target
    ):
        """A driver that crashed after the roots landed (S15): any tick
        finishes the static phase."""
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        build_id, _ = await _plan(registry, [root], expand=False)
        frontier = registry.build_get_frontier(build_id)
        assert [m.task_id for m in frontier.discovery_jobs] == [str(root.id)]
        assert frontier.sealed is False
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry, workers=True)
        )
        assert summary.discovered == 1
        assert summary.terminal_status == "completed"
        (plan,) = registry.plans.values()
        assert plan.sealed_at is not None

    async def test_a_class_this_code_cannot_import_excludes_the_member(
        self, default_in_memory_fs_target: Target
    ):
        """S34: the member is excluded (discovery_failed), the excluded root
        fails the build, and it is never a discovery job again."""
        registry = InMemoryRegistry()
        root = SyncOnlyTask(name=f"gone-{new_id()}")
        build_id, _ = await _plan(registry, [root], expand=False)
        (instance,) = registry.instances.values()
        instance.body["__name"] = "NoSuchClass"
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.excluded == 1
        (exclude,) = registry.calls_to("member_discovery_failed")
        assert "NoSuchClass" in exclude["error"]
        assert registry.builds[build_id].status == "failed"

    async def test_a_requires_that_raises_excludes_the_member(
        self, default_in_memory_fs_target: Target
    ):
        class Exploding(SyncOnlyTask):
            def requires(self):
                raise RuntimeError("exploded")

        registry = InMemoryRegistry()
        deployment = registry.add_deployment(app_name="app")
        root = Exploding(name=f"boom-{new_id()}")
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        from datetime import datetime, timezone

        from stardag.build._registration import registration_item

        registry.plan_create(
            build_id,
            plan_id=new_id(),
            deployment_id=deployment,
            settings={},
            roots=[
                registration_item(
                    root,
                    declared_upstreams=None,
                    observed_complete=False,
                    observed_at=datetime.now(timezone.utc),
                )
            ],
        )
        registry.build_set_reactive_meta(build_id, app_name="app")
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.excluded == 1
        assert "exploded" in registry.calls_to("member_discovery_failed")[0]["error"]


class TestRefusals:
    async def test_a_claim_held_elsewhere_is_counted_not_failed(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"held-{new_id()}")
        build_id, deployment = await _plan(registry, [task])
        # Another build's execution holds it.
        other = registry.build_create(root_task_ids=[str(task.id)]).id
        walk = await walk_aio([task])
        plan = await register_plan_aio(
            registry, other, walk, deployment_id=deployment, settings={"X": "1"}
        )
        registry.member_start(plan.id, str(task.id), execution_id=new_id())
        config = TickConfig(linger_seconds=0, poll_interval_seconds=0.01)
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry), config
        )
        assert summary.outcome == "lingered_out"
        assert summary.spawned == 0
        assert registry.builds[build_id].status == "running"

    async def test_a_full_limit_is_counted_and_the_build_waits(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        registry.limits["gpu"] = 0
        task = SyncOnlyTask(name=f"limited-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        config = TickConfig(
            linger_seconds=0,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry), config
        )
        assert summary.limit_denied == 1
        assert registry.builds[build_id].status == "running"
        # The refusal recorded the keys, so a freed slot finds the task.
        assert registry.tasks[str(task.id)].limit_keys == {"gpu"}

    async def test_a_limit_is_configured_through_the_client_seam(
        self, default_in_memory_fs_target: Target
    ):
        """``concurrency_limit_set/delete/list``: the fake follows the
        server's seam, and the cap it sets is the one a claim meets."""
        registry = InMemoryRegistry()
        registry.concurrency_limit_set("gpu", 0)
        assert registry.concurrency_limit_list() == {"gpu": 0}
        task = SyncOnlyTask(name=f"limited-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        config = TickConfig(
            linger_seconds=0,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )
        executor = FakeDetachedExecutor(registry=registry)
        summary = await _tick(registry, build_id, executor, config)
        assert summary.limit_denied == 1
        registry.concurrency_limit_delete("gpu")
        assert registry.concurrency_limit_list() == {}
        with pytest.raises(NotFoundError):
            registry.concurrency_limit_delete("gpu")
        summary = await _tick(registry, build_id, executor, config)
        assert summary.spawned == 1

    async def test_a_spawn_failing_every_attempt_is_recorded_and_the_build_stalls(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"no-spawn-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        executor = FakeDetachedExecutor(
            registry=registry, spawn_error=RuntimeError("refused")
        )
        summary = await _tick(registry, build_id, executor)
        assert summary.spawn_failed == 1
        (fail,) = registry.calls_to("member_fail", task_id=task.id)
        assert "refused" in fail["error_message"]
        assert summary.terminal_status == "failed"
        assert registry.builds[build_id].status == "failed"

    @pytest.mark.parametrize(
        "code", ["execution_not_current", "not_claim_holder", "unknown_execution"]
    )
    async def test_an_orphaned_spawn_is_stopped(
        self, code: str, default_in_memory_fs_target: Target
    ):
        """Every "this execution is over" refusal of the ref record — the
        claim moved to another execution or another plan, or the ledger has
        no such execution — stops the container just spawned."""

        class RefusesRefStart(InMemoryRegistry):
            def member_start(self, plan_id, task_id, **kwargs):
                if not kwargs.get("claim", True):
                    raise refuse(code)
                return super().member_start(plan_id, task_id, **kwargs)

        registry = RefusesRefStart()
        task = SyncOnlyTask(name=f"orphan-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        executor = FakeDetachedExecutor(registry=registry)
        config = TickConfig(linger_seconds=0, poll_interval_seconds=0.01)
        summary = await _tick(registry, build_id, executor, config)
        assert summary.cancelled_refs == 1
        assert executor.cancel_detached_calls

    async def test_a_superseded_plan_stops_the_tick(
        self, default_in_memory_fs_target: Target
    ):
        class Superseded(InMemoryRegistry):
            def member_start(self, plan_id, task_id, **kwargs):
                if kwargs.get("claim", True):
                    raise refuse("plan_superseded")
                return super().member_start(plan_id, task_id, **kwargs)

        registry = Superseded()
        build_id, _ = await _plan(registry, [SyncOnlyTask(name=f"s-{new_id()}")])
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.outcome == "superseded"


class TestTheEnd:
    async def test_a_stalled_build_fails_and_skips_what_the_failure_blocks(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        build_id, _ = await _plan(registry, [root])
        registry.tasks[str(leaf.id)].status = "failed"
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.terminal_status == "failed"
        assert registry.builds[build_id].status == "failed"
        assert summary.skipped == 2
        assert registry.status_of(mid.id) == registry.status_of(root.id) == "skipped"

    async def test_a_terminal_build_is_reported_as_it_is(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        build_id, _ = await _plan(registry, [SyncOnlyTask(name=f"t-{new_id()}")])
        registry.build_cancel(build_id)
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert (summary.outcome, summary.terminal_status) == ("terminal", "cancelled")
        assert summary.spawned == 0

    async def test_a_build_that_is_not_reactive_is_not_driven(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        build_id, _ = await _plan(
            registry, [SyncOnlyTask(name=f"r-{new_id()}")], reactive=False
        )
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.outcome == "not_reactive"

    async def test_the_summary_is_reported(self, default_in_memory_fs_target: Target):
        registry = InMemoryRegistry()
        build_id, _ = await _plan(registry, [SyncOnlyTask(name=f"sum-{new_id()}")])
        await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry, workers=True)
        )
        (reported,) = registry.tick_summaries[build_id]
        assert reported["outcome"] == "terminal"
        assert reported["spawned"] == 1


class TestLeaseAndHandshake:
    async def test_a_release_reports_whether_the_caller_held_the_lease(self):
        """The fake follows the server: an owner-checked release answers
        ``held`` -- a stranger's release clears nothing and says so."""
        registry = InMemoryRegistry()
        build_id = registry.build_create(root_task_ids=["t"]).id
        registry.scheduler_lease_acquire(build_id, owner_id="a", ttl_seconds=60)
        assert registry.scheduler_lease_release(build_id, owner_id="b").held is False
        assert registry.scheduler_lease_renew(
            build_id, owner_id="a", ttl_seconds=60
        ).held
        assert registry.scheduler_lease_release(build_id, owner_id="a").held is True

    async def test_a_held_lease_is_a_no_op(self, default_in_memory_fs_target: Target):
        registry = InMemoryRegistry()
        build_id, _ = await _plan(registry, [SyncOnlyTask(name=f"l-{new_id()}")])
        registry.scheduler_lease_acquire(build_id, owner_id="someone", ttl_seconds=60)
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.outcome == "lease_held"
        assert not registry.called("member_start")

    async def test_a_flag_set_at_the_deadline_extends_the_linger(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"h-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        registry.limits["slot"] = 0
        config = TickConfig(
            linger_seconds=0.05,
            poll_interval_seconds=0.02,
            limit_key_selector=lambda t: ["slot"],
        )

        async def free_the_slot():
            await asyncio.sleep(0.03)
            registry.limits["slot"] = 1
            registry.build_notify(build_id)

        freeing = asyncio.create_task(free_the_slot())
        summary = await _tick(
            registry,
            build_id,
            FakeDetachedExecutor(registry=registry, workers=True),
            config,
        )
        await freeing
        assert summary.terminal_status == "completed"

    async def test_neighbours_are_drained_after_a_pass_that_acted(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        build_id, _ = await _plan(registry, [SyncOnlyTask(name=f"d-{new_id()}")])
        neighbour = registry.build_create(root_task_ids=["x"]).id
        registry.build_set_reactive_meta(neighbour, app_name="other-app")
        registry.builds[neighbour].needs_tick = True
        spawned: list[tuple[UUID, str]] = []
        config = TickConfig(
            linger_seconds=0.3,
            poll_interval_seconds=0.01,
            spawn_tick=lambda b, a: spawned.append((b, a)),
        )
        summary = await _tick(
            registry,
            build_id,
            FakeDetachedExecutor(registry=registry, workers=True),
            config,
        )
        assert (neighbour, "other-app") in spawned
        assert summary.neighbour_ticks_spawned >= 1

    async def test_a_lease_lost_mid_pass_stops_claiming(
        self, default_in_memory_fs_target: Target
    ):
        """The lease is checked before every claim, not only before the
        pass: once a renewal is lost mid-pass, another tick may hold it, so
        this pass claims nothing further and exits ``lease_lost``."""
        from stardag.build._reactive._lease import SchedulerLease

        registry = InMemoryRegistry()
        tasks = [SyncOnlyTask(name=f"wide-{i}-{new_id()}") for i in range(4)]
        build_id, _ = await _plan(registry, list(tasks))

        class LosesLeaseOnFirstSpawn(FakeDetachedExecutor):
            async def submit_detached(self, task, *, execution_id):
                handle = await super().submit_detached(task, execution_id=execution_id)
                # The background renewal is refused while the pass runs.
                for lease in leases:
                    lease._lost = True
                return handle

        leases: list[SchedulerLease] = []
        original_aenter = SchedulerLease.__aenter__

        async def recording_aenter(self):
            leases.append(self)
            return await original_aenter(self)

        config = TickConfig(
            linger_seconds=0, poll_interval_seconds=0.01, max_concurrent_actions=1
        )
        executor = LosesLeaseOnFirstSpawn(registry=registry)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(SchedulerLease, "__aenter__", recording_aenter)
            summary = await _tick(registry, build_id, executor, config)
        assert summary.outcome == "lease_lost"
        claims = [c for c in registry.calls_to("member_start") if c["claim"]]
        assert len(claims) == 1
        assert summary.spawned == 1

    @staticmethod
    def _recording_leases(mp: pytest.MonkeyPatch) -> list:
        """Every lease a tick takes, so a test can lose it mid-action."""
        from stardag.build._reactive._lease import SchedulerLease

        leases: list[SchedulerLease] = []
        original_aenter = SchedulerLease.__aenter__

        async def recording_aenter(self):
            leases.append(self)
            return await original_aenter(self)

        mp.setattr(SchedulerLease, "__aenter__", recording_aenter)
        return leases

    async def test_a_lease_lost_during_the_metadata_await_takes_no_claim(
        self, default_in_memory_fs_target: Target, monkeypatch: pytest.MonkeyPatch
    ):
        """Re-checked after ``get_executor_metadata()``: an await between
        the entry check and the claim can outlive the lease."""
        registry = InMemoryRegistry()
        (task,) = [SyncOnlyTask(name=f"one-{new_id()}")]
        build_id, _ = await _plan(registry, [task])
        leases = self._recording_leases(monkeypatch)

        class LosesLeaseInMetadata(FakeDetachedExecutor):
            async def get_executor_metadata(self, task):
                for lease in leases:
                    lease._lost = True
                return None

        summary = await _tick(
            registry, build_id, LosesLeaseInMetadata(registry=registry)
        )
        assert summary.outcome == "lease_lost"
        assert not [c for c in registry.calls_to("member_start") if c["claim"]]

    async def test_a_lease_lost_during_discovery_registers_nothing(
        self, default_in_memory_fs_target: Target, monkeypatch: pytest.MonkeyPatch
    ):
        """Re-checked before ``register_members_aio``: discovery runs user
        code and completion checks, and can outlive the lease."""
        from stardag.build._reactive import _frontier_actions

        registry = InMemoryRegistry()
        _, _, root = _chain()
        build_id, _ = await _plan(registry, [root], expand=False)
        leases = self._recording_leases(monkeypatch)
        original_walk = _frontier_actions.walk_aio

        async def outliving_walk(*args, **kwargs):
            walk = await original_walk(*args, **kwargs)
            for lease in leases:
                lease._lost = True
            return walk

        monkeypatch.setattr(_frontier_actions, "walk_aio", outliving_walk)
        registered = len(registry.calls_to("plan_register_members"))
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.outcome == "lease_lost"
        assert summary.discovered == 0
        assert len(registry.calls_to("plan_register_members")) == registered

    async def test_a_lease_lost_during_a_failing_discovery_excludes_nothing(
        self, default_in_memory_fs_target: Target, monkeypatch: pytest.MonkeyPatch
    ):
        """And before an exclusion: a discovery that failed after the lease
        was lost is left to the tick that holds it."""
        from stardag.build._reactive import _frontier_actions
        from stardag.exceptions import StardagError

        registry = InMemoryRegistry()
        _, _, root = _chain()
        build_id, _ = await _plan(registry, [root], expand=False)
        leases = self._recording_leases(monkeypatch)

        async def failing_walk(*args, **kwargs):
            for lease in leases:
                lease._lost = True
            raise StardagError("cannot state this member")

        monkeypatch.setattr(_frontier_actions, "walk_aio", failing_walk)
        summary = await _tick(
            registry, build_id, FakeDetachedExecutor(registry=registry)
        )
        assert summary.outcome == "lease_lost"
        assert summary.excluded == 0
        assert not registry.called("member_discovery_failed")
        assert not registry.called("member_exclude")


class TestRollover:
    async def _rolled_setup(self, registry: InMemoryRegistry, roots):
        old = registry.add_deployment(app_name="app", code_id="v1")
        build_id, _ = await _plan(registry, roots, deployment_id=old)
        new = registry.add_deployment(app_name="app", code_id="v2")
        return build_id, old, new

    async def test_a_tick_of_the_current_deployment_rolls_the_build_over(
        self, default_in_memory_fs_target: Target
    ):
        """S3/S6: a new plan under the new deployment supersedes the old one
        at its seal, and the build goes on under it."""
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        build_id, old, new = await self._rolled_setup(registry, [root])

        async def roll_over(frontier: BuildFrontier):
            return await roll_over_aio(registry, frontier, own_deployment_id=new)

        summary = await _tick(
            registry,
            build_id,
            FakeDetachedExecutor(registry=registry, workers=True),
            deployment_id=new,
            roll_over=roll_over,
        )
        assert summary.rolled_over == 1
        assert summary.terminal_status == "completed"
        plans = sorted(registry.plans.values(), key=lambda p: p.generation)
        assert [p.deployment_id for p in plans] == [old, new]
        assert plans[0].superseded_at is not None and plans[1].active

    async def test_a_tick_of_an_older_deployment_is_superseded(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        build_id, old, new = await self._rolled_setup(
            registry, [SyncOnlyTask(name=f"o-{new_id()}")]
        )
        # A newer deploy (v3) is current: the v2 tick must not move the
        # build — rollover only moves forward, on the registry's record.
        registry.add_deployment(app_name="app", code_id="v3")

        async def roll_over(frontier: BuildFrontier):
            return await roll_over_aio(registry, frontier, own_deployment_id=new)

        summary = await _tick(
            registry,
            build_id,
            FakeDetachedExecutor(registry=registry),
            deployment_id=new,
            roll_over=roll_over,
        )
        assert summary.outcome == "superseded"
        assert len(registry.plans) == 1

    async def test_without_a_rollover_hook_another_deployments_plan_supersedes(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        build_id, old, new = await self._rolled_setup(
            registry, [SyncOnlyTask(name=f"n-{new_id()}")]
        )
        summary = await _tick(
            registry,
            build_id,
            FakeDetachedExecutor(registry=registry),
            deployment_id=new,
        )
        assert summary.outcome == "superseded"

    async def test_roots_whose_ids_change_under_the_new_code_fail_the_build(
        self, default_in_memory_fs_target: Target
    ):
        """S20: re-trigger it as a new build."""
        registry = InMemoryRegistry()
        root = SyncOnlyTask(name=f"renamed-{new_id()}")
        build_id, old, new = await self._rolled_setup(registry, [root])
        (instance,) = [
            i for i in registry.instances.values() if i.task_id == str(root.id)
        ]
        instance.body["name"] = "something-else"

        async def roll_over(frontier: BuildFrontier):
            return await roll_over_aio(registry, frontier, own_deployment_id=new)

        summary = await _tick(
            registry,
            build_id,
            FakeDetachedExecutor(registry=registry),
            deployment_id=new,
            roll_over=roll_over,
        )
        assert summary.outcome == "rollover_failed"
        assert registry.builds[build_id].status == "failed"
        assert "new build" in (registry.builds[build_id].error_message or "")


async def test_a_member_registered_twice_is_a_no_op(
    default_in_memory_fs_target: Target,
):
    """Idempotency (STA-54): a chunk re-delivered changes nothing."""
    registry = InMemoryRegistry()
    task = SyncOnlyTask(name=f"idem-{new_id()}")
    build_id, _ = await _plan(registry, [task])
    (plan,) = registry.plans.values()
    walk = await walk_aio([task])
    before = len(registry.events)
    await register_members_aio(registry, plan.id, walk.items())
    assert len(registry.events) == before


pytestmark = pytest.mark.asyncio
