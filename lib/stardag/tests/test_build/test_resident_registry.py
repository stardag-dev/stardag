"""The resident engines on the v2 registry (the ``resident`` test tier).

Both engines (``build_aio`` and the sequential one) drive the registry
through one session, so the promises are asserted for both:

- the static phase: roots first (unexpanded), the walk in post-order
  chunks, sealed — all before any execution claims;
- every execution claims (D11), in-process with a short TTL renewed while
  it runs, and every report names its execution;
- a yield is registered in one ``/yield`` with ``suspend: false`` (the
  in-process generator waits, its claim kept);
- resume reuses the build's plan for the scope and resets failed members;
- a walk that fails (an instance conflict, a ``requires()`` that raises)
  fails before any build exists;
- settings are applied for the build's duration; reserved keys refused.
"""

from __future__ import annotations

import asyncio
import os
import time
import typing
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import pytest

import stardag as sd
from stardag import Task, auto_namespace
from stardag.build import (
    BuildExitStatus,
    ClaimConfig,
    FailMode,
    RequiresError,
    SettingsError,
    build_aio,
    build_sequential,
    build_sequential_aio,
)
from stardag.build._deployment import DeploymentResolutionError
from stardag.build._registration import new_id
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.dynamic_deps_dag import DynamicDepsTask
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]


async def _sync_sequential(tasks, **kwargs):
    return await asyncio.to_thread(build_sequential, tasks, **kwargs)


ENGINES = pytest.mark.parametrize(
    "engine",
    [build_aio, build_sequential_aio, _sync_sequential],
    ids=["concurrent", "sequential_aio", "sequential"],
)


def _chain() -> tuple[SyncOnlyTask, SyncOnlyTask, SyncOnlyTask]:
    leaf = SyncOnlyTask(name=f"leaf-{new_id()}")
    mid = SyncOnlyTask(name="mid", deps=(leaf,))
    root = SyncOnlyTask(name="root", deps=(mid,))
    return leaf, mid, root


@ENGINES
class TestStaticPhase:
    async def test_roots_first_then_post_order_then_seal_before_any_claim(
        self, engine, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        summary = await engine([root], registry=registry)
        assert summary.status == BuildExitStatus.SUCCESS

        methods = registry.methods_called()
        first_claim = methods.index("member_start")
        assert methods.index("plan_create") < methods.index("plan_register_members")
        assert methods.index("plan_seal") < first_claim
        (created,) = registry.calls_to("plan_create")
        assert [r.task_id for r in created["roots"]] == [str(root.id)]
        assert all(r.declared_upstreams is None for r in created["roots"])
        items = [
            i.task_id
            for call in registry.calls_to("plan_register_members")
            for i in call["items"]
        ]
        assert (
            items.index(str(leaf.id))
            < items.index(str(mid.id))
            < items.index(str(root.id))
        )
        for task in (leaf, mid, root):
            assert registry.status_of(task.id) == "completed"

    async def test_a_complete_task_is_observed_and_not_expanded(
        self, engine, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        mid.target().save({"pre": "existing"})
        summary = await engine([root], registry=registry)
        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        items = {
            i.task_id: i
            for call in registry.calls_to("plan_register_members")
            for i in call["items"]
        }
        assert items[str(mid.id)].observed_complete is True
        assert items[str(mid.id)].declared_upstreams is None
        # The walk stopped at the complete task: its upstream is not planned.
        assert str(leaf.id) not in items
        assert not registry.called("member_start", task_id=mid.id)

    async def test_every_execution_claims_and_its_reports_name_it(
        self, engine, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        config = ClaimConfig(in_process_ttl_seconds=77)
        leaf, mid, root = _chain()
        await engine([root], registry=registry, claim_config=config)
        for task in (leaf, mid, root):
            (claim,) = registry.calls_to("member_start", task_id=task.id)
            assert claim["claim"] is True
            assert claim["claim_ttl_seconds"] == 77
            (complete,) = registry.calls_to("member_complete", task_id=task.id)
            assert complete["execution_id"] == claim["execution_id"]
            execution = registry.executions[claim["execution_id"]]
            assert execution.outcome == "completed"

    async def test_a_yield_is_sent_once_with_suspend_false_and_keeps_the_claim(
        self, engine, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        child = DynamicDepsTask(value=f"child-{new_id()}")
        parent = DynamicDepsTask(value="parent", dynamic_deps=(child,))
        summary = await engine([parent], registry=registry)
        assert summary.status == BuildExitStatus.SUCCESS

        (yield_call,) = registry.calls_to("member_yield", task_id=parent.id)
        assert yield_call["suspend"] is False
        assert yield_call["yielded"] == [str(child.instance_hash)]
        assert str(child.id) in {i.task_id for i in yield_call["items"]}
        # One execution of the parent: claimed once, completed by it.
        claims = registry.calls_to("member_start", task_id=parent.id)
        assert len(claims) == 1
        assert yield_call["execution_id"] == claims[0]["execution_id"]
        assert registry.status_of(parent.id) == "completed"
        assert registry.status_of(child.id) == "completed"


@ENGINES
class TestBoundaries:
    async def test_resume_reuses_the_plan_and_resets_failed_members(
        self, engine, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        first = await engine([root], registry=registry, resume_build_id=build_id)
        assert first.status == BuildExitStatus.SUCCESS
        (plan_id,) = registry.plans
        # A member failed under another execution, and its output — and its
        # downstream's — vanished.
        registry.tasks[str(mid.id)].status = "failed"
        registry.builds[build_id].status = "failed"
        for task in (mid, root):
            InMemoryFileTarget.uri_to_bytes.pop(task.target().uri, None)
        second = await engine([root], registry=registry, resume_build_id=build_id)
        assert second.build_id == build_id
        assert list(registry.plans) == [plan_id]
        assert registry.called("member_retry", task_id=mid.id)
        assert registry.builds[build_id].status == "completed"

    async def test_a_failing_requires_fails_before_any_build_exists(
        self, engine, default_in_memory_fs_target: Target
    ):
        class Exploding(SyncOnlyTask):
            def requires(self):
                raise RuntimeError("requires() exploded")

        registry = InMemoryRegistry()
        with pytest.raises(RequiresError, match="exploded"):
            await engine([Exploding(name="boom")], registry=registry)
        assert registry.builds == {}

    async def test_an_instance_conflict_fails_before_any_build_exists(
        self, engine, default_in_memory_fs_target: Target
    ):
        class Labelled(Task[str]):
            value: str
            label: Annotated[str, sd.StardagField(significant=False)] = "a"

            def run(self):
                self._save(self.value)

        class Pair(Task[str]):
            first: sd.TaskLoads[str]
            second: sd.TaskLoads[str]

            def requires(self):
                return (self.first, self.second)

            def run(self):
                self._save("pair")

        registry = InMemoryRegistry()
        pair = Pair(
            first=Labelled(value="x", label="nightly"),
            second=Labelled(value="x", label="backfill"),
        )
        with pytest.raises(sd.InstanceConflictError):
            await engine([pair], registry=registry)
        assert registry.builds == {}

    async def test_settings_are_applied_for_the_build(
        self, engine, default_in_memory_fs_target: Target
    ):
        seen: dict[str, str | None] = {}

        class ReadsSettings(Task[str]):
            name: str

            def run(self):
                seen[self.name] = os.environ.get("MY_FEATURE_FLAG")
                self._save("ok")

        registry = InMemoryRegistry()
        task = ReadsSettings(name=f"reads-{new_id()}")
        await engine([task], registry=registry, settings={"MY_FEATURE_FLAG": "on"})
        assert seen[task.name] == "on"
        assert "MY_FEATURE_FLAG" not in os.environ
        (plan,) = registry.plans.values()
        assert registry.settings[plan.settings_hash] == {"MY_FEATURE_FLAG": "on"}

    async def test_a_reserved_settings_key_is_refused(
        self, engine, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        with pytest.raises(SettingsError, match="reserved"):
            await engine(
                [SyncOnlyTask(name="x")],
                registry=registry,
                settings={"STARDAG_PLAN_ID": "nope"},
            )
        assert registry.builds == {}


def _claimed_elsewhere(
    registry: InMemoryRegistry, task: SyncOnlyTask
) -> tuple[UUID, UUID]:
    """Another build plans ``task`` and one of its executions claims it (a
    long TTL, so it never lapses in the test). Returns (plan, execution)."""
    from datetime import datetime, timezone

    from stardag.build._registration import registration_item

    build_id = registry.build_create(root_task_ids=[str(task.id)]).id
    deployment = registry.add_deployment(kind="local", code_id="other")
    item = registration_item(
        task,
        declared_upstreams=[],
        observed_complete=False,
        observed_at=datetime.now(timezone.utc),
    )
    root = item.model_copy(update={"declared_upstreams": None})
    plan = registry.plan_create(
        build_id, plan_id=new_id(), deployment_id=deployment, settings={}, roots=[root]
    )
    registry.plan_register_members(plan.id, [item])
    execution_id = new_id()
    registry.member_start(
        plan.id, str(task.id), execution_id=execution_id, claim_ttl_seconds=3600
    )
    return plan.id, execution_id


_FAST_WAIT = ClaimConfig(
    wait_initial_interval_seconds=0.05, wait_max_interval_seconds=0.05
)


class TestClaims:
    async def test_a_claim_held_elsewhere_is_waited_out(
        self, default_in_memory_fs_target: Target
    ):
        """S1/S25: the claim decides who runs; the other build waits on the
        global status and reuses the result."""
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"held-{new_id()}")
        plan_id, execution_id = _claimed_elsewhere(registry, task)

        async def other_execution_finishes():
            await asyncio.sleep(0.2)
            task.target().save({"name": task.name, "mode": "elsewhere"})
            registry.member_complete(plan_id, str(task.id), execution_id=execution_id)

        finisher = asyncio.create_task(other_execution_finishes())
        summary = await build_aio([task], registry=registry, claim_config=_FAST_WAIT)
        await finisher
        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 0
        assert task.target().load()["mode"] == "elsewhere"
        # This build never held an execution of the task.
        assert summary.build_id is not None
        own_plan = registry.active_plan(summary.build_id)
        assert own_plan is not None
        assert not any(e.plan_id == own_plan.id for e in registry.executions.values())

    async def test_a_claim_never_released_times_out_as_a_local_failure(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"stuck-{new_id()}")
        _claimed_elsewhere(registry, task)
        config = ClaimConfig(
            wait_timeout_seconds=0.2,
            wait_initial_interval_seconds=0.05,
            wait_max_interval_seconds=0.05,
        )
        summary = await build_aio(
            [task], registry=registry, claim_config=config, fail_mode=FailMode.CONTINUE
        )
        assert summary.status == BuildExitStatus.FAILURE
        assert "timed out" in str(summary.error)
        # Nothing was reported against a task another execution holds.
        assert not registry.called("member_fail", task_id=task.id)
        assert registry.status_of(task.id) == "running"

    async def test_an_in_process_claim_is_renewed_while_it_runs(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()

        class Slow(Task[str]):
            name: str

            def run(self):
                time.sleep(0.3)
                self._save("done")

        task = Slow(name=f"slow-{new_id()}")
        config = ClaimConfig(in_process_ttl_seconds=60, renew_interval_seconds=0.05)
        await build_aio([task], registry=registry, claim_config=config)
        (claim,) = registry.calls_to("member_start", task_id=task.id)
        renewals = registry.calls_to("claim_renew", task_id=task.id)
        assert renewals, "the claim was never renewed"
        assert {r["execution_id"] for r in renewals} == {claim["execution_id"]}

    async def test_limit_keys_travel_on_the_claim(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"limited-{new_id()}")
        await build_aio(
            [task], registry=registry, limit_key_selector=lambda t: ["gpu", "gpu"]
        )
        (claim,) = registry.calls_to("member_start", task_id=task.id)
        assert claim["limit_keys"] == ["gpu", "gpu"]
        assert registry.tasks[str(task.id)].limit_keys == {"gpu"}


class TestDeployments:
    async def test_a_local_build_plans_under_one_local_deployment_per_code_id(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        await build_aio([SyncOnlyTask(name=f"a-{new_id()}")], registry=registry)
        await build_aio([SyncOnlyTask(name=f"b-{new_id()}")], registry=registry)
        (deployment,) = registry.deployments.values()
        assert (deployment.kind, deployment.code_id) == ("local", "test-code")
        assert deployment.activated_at is not None
        assert {p.deployment_id for p in registry.plans.values()} == {deployment.id}

    async def test_a_build_whose_tasks_run_on_an_app_plans_under_its_current_deployment(
        self, default_in_memory_fs_target: Target
    ):
        """D13: a hybrid driver plans under the app's current deployment."""
        from stardag.build import HybridConcurrentTaskExecutor

        class OnApp(HybridConcurrentTaskExecutor):
            def deployment_app_name(self) -> str:
                return "my-app"

        registry = InMemoryRegistry()
        registry.add_deployment(app_name="my-app", code_id="v1")
        current = registry.add_deployment(app_name="my-app", code_id="v2")
        registry.add_deployment(app_name="my-app", code_id="v3", activated=False)
        await build_aio(
            [SyncOnlyTask(name=f"h-{new_id()}")],
            registry=registry,
            task_executor=OnApp(),
        )
        (plan,) = registry.plans.values()
        assert plan.deployment_id == current

    async def test_an_app_without_a_deployment_cannot_host_the_plan(
        self, default_in_memory_fs_target: Target
    ):
        from stardag.build import HybridConcurrentTaskExecutor

        class OnApp(HybridConcurrentTaskExecutor):
            def deployment_app_name(self) -> str:
                return "never-deployed"

        with pytest.raises(DeploymentResolutionError, match="never-deployed"):
            await build_aio(
                [SyncOnlyTask(name="x")],
                registry=InMemoryRegistry(),
                task_executor=OnApp(),
            )

    async def test_a_deployed_container_plans_under_its_own_deployment(
        self, default_in_memory_fs_target: Target, monkeypatch: pytest.MonkeyPatch
    ):
        registry = InMemoryRegistry()
        own = registry.add_deployment(app_name="my-app")
        monkeypatch.setenv("STARDAG_DEPLOYMENT_ID", str(own))
        await build_aio([SyncOnlyTask(name=f"own-{new_id()}")], registry=registry)
        (plan,) = registry.plans.values()
        assert plan.deployment_id == own


class TestChunking:
    async def test_members_are_sent_in_chunks_of_at_most_1000(
        self, default_in_memory_fs_target: Target
    ):
        leaves = [SyncOnlyTask(name=f"leaf-{i}") for i in range(1200)]
        for leaf in leaves:
            leaf.target().save({"pre": True})
        root = SyncOnlyTask(name="wide-root", deps=tuple(leaves))
        registry = InMemoryRegistry()
        summary = await build_aio([root], registry=registry)
        assert summary.status == BuildExitStatus.SUCCESS
        sizes = [len(c["items"]) for c in registry.calls_to("plan_register_members")]
        assert sizes == [1000, 201]


class TestConcurrentSettings:
    async def test_two_builds_with_different_settings_in_one_process_are_refused(
        self, default_in_memory_fs_target: Target
    ):
        started = asyncio.Event()
        release = asyncio.Event()

        class Waits(Task[str]):
            name: str

            async def run_aio(self):
                started.set()
                await release.wait()
                self._save("ok")

        first = asyncio.create_task(
            build_aio(
                [Waits(name="w1")], registry=InMemoryRegistry(), settings={"A": "1"}
            )
        )
        await started.wait()
        try:
            with pytest.raises(SettingsError, match="different settings"):
                await build_aio(
                    [SyncOnlyTask(name="w2")],
                    registry=InMemoryRegistry(),
                    settings={"A": "2"},
                )
        finally:
            release.set()
            await first


def test_the_ttl_of_a_lapsed_claim_is_taken_over(default_in_memory_fs_target: Target):
    """S21: a claim that lapsed (its holder died) is taken over by the next
    claiming start, which closes the old execution's claim."""
    registry = InMemoryRegistry()
    task = SyncOnlyTask(name=f"lapsed-{new_id()}")
    build_id = registry.build_create(root_task_ids=[str(task.id)]).id
    deployment = registry.add_deployment(kind="local", code_id="c")
    from stardag.build._registration import registration_item
    from datetime import datetime, timezone

    item = registration_item(
        task,
        declared_upstreams=[],
        observed_complete=False,
        observed_at=datetime.now(timezone.utc),
    )
    root = item.model_copy(update={"declared_upstreams": None})
    plan = registry.plan_create(
        build_id, plan_id=new_id(), deployment_id=deployment, settings={}, roots=[root]
    )
    registry.plan_register_members(plan.id, [item])
    dead: UUID = new_id()
    registry.member_start(
        plan.id, str(task.id), execution_id=dead, claim_ttl_seconds=60
    )
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    registry.clock = lambda: later
    alive = new_id()
    registry.member_start(plan.id, str(task.id), execution_id=alive)
    assert registry.executions[dead].claim_outcome == "taken_over"
    assert registry.tasks[str(task.id)].execution_id == alive


@ENGINES
async def test_the_stability_check_runs_only_when_something_is_registered(
    engine, default_in_memory_fs_target: Target
):
    """The round trip guards registry rehydration, so a build without a
    registry skips it (an ``AliasTask`` body, refused by the check, still
    builds locally); with a registry every walked instance is checked."""
    from unittest import mock

    from stardag.registry import NoOpRegistry

    with mock.patch(
        "stardag.build._registration.check_serialization_stability"
    ) as check:
        _, _, root = _chain()
        await engine([root], registry=NoOpRegistry())
        assert check.call_count == 0
        _, _, root = _chain()
        await engine([root], registry=InMemoryRegistry())
        assert check.call_count == 3


@ENGINES
@pytest.mark.parametrize(
    "given,expected",
    [(None, {"MY_FEATURE_FLAG": "on"}), ({}, None)],
    ids=["bare-reuses-stored", "explicit-empty"],
)
async def test_a_bare_resume_reuses_the_active_plans_settings(
    engine, given, expected, default_in_memory_fs_target: Target
):
    """``settings`` omitted on a resume means the build's own settings (its
    active plan's, read from the registry), so a bare resume stays in its
    scope; an explicit ``{}`` means none, and plans a new scope (S14)."""
    seen: list[str | None] = []

    class ReadsFlag(Task[str]):
        name: str

        def run(self):
            seen.append(os.environ.get("MY_FEATURE_FLAG"))
            self._save("ok")

    registry = InMemoryRegistry()
    first = ReadsFlag(name=f"first-{new_id()}")
    summary = await engine(
        [first], registry=registry, settings={"MY_FEATURE_FLAG": "on"}
    )
    assert summary.build_id is not None
    first_plan = registry.active_plan(summary.build_id)
    assert first_plan is not None
    await engine(
        [ReadsFlag(name=first.name)],
        registry=registry,
        resume_build_id=summary.build_id,
        settings=given,
    )
    active = registry.active_plan(summary.build_id)
    assert active is not None
    assert seen == ["on"]  # the first run; the resume found it complete
    if expected is None:
        assert active.id != first_plan.id
        assert registry.settings[active.settings_hash] == {}
    else:
        assert active.id == first_plan.id
        assert registry.settings[active.settings_hash] == expected
