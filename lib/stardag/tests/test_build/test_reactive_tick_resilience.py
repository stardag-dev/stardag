"""The tick under registry pressure (STA-135): one member's failure does not
abandon its siblings' claims, a ref the registry did not answer for is not
an orphan, a tick dying of an outage leaves its build flagged, and spawns
are admitted adaptively under the rate limit."""

from __future__ import annotations

import asyncio
import typing

import pytest

from stardag import BaseTask, auto_namespace
from stardag.build import TickConfig
from stardag.build._reactive import _frontier_actions, _tick
from stardag.build._reactive._frontier_actions import Admission
from stardag.build._registration import new_id
from stardag.exceptions import APIError, QuotaExceededError, RateLimitError
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import refuse
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.fakes import FakeDetachedExecutor
from tests.test_build.test_reactive_tick import _plan, _tick as tick

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]

ONE_PASS = TickConfig(linger_seconds=0, poll_interval_seconds=0.01)


def _outage() -> APIError:
    return APIError("Registry call failed", status_code=503, detail="unavailable")


class _SlowSpawns(FakeDetachedExecutor):
    """Spawns that take a moment, so siblings are mid-spawn when one
    member's claim fails."""

    async def submit_detached(self, task, *, execution_id):
        await asyncio.sleep(0.05)
        return await super().submit_detached(task, execution_id=execution_id)


def _ref_records(registry: InMemoryRegistry, task) -> list[dict]:
    return [
        c for c in registry.calls_to("member_start", task_id=task.id) if not c["claim"]
    ]


async def test_one_failing_claim_does_not_abandon_its_siblings(
    default_in_memory_fs_target: Target,
):
    """Fail-fast cancelled every sibling between its claim and its spawn,
    leaving claims with no container and no ref until their TTL lapsed."""
    a, b, c = (SyncOnlyTask(name=f"{n}-{new_id()}") for n in "abc")

    class FailsOneClaim(InMemoryRegistry):
        def member_start(self, plan_id, task_id, **kwargs):
            if kwargs.get("claim", True) and task_id == str(b.id):
                self._record("member_start", plan_id=plan_id, task_id=task_id, **kwargs)
                raise _outage()
            return super().member_start(plan_id, task_id, **kwargs)

    registry = FailsOneClaim()
    build_id, _ = await _plan(registry, [a, b, c])
    executor = _SlowSpawns(registry=registry)
    with pytest.raises(APIError):
        await tick(registry, build_id, executor, ONE_PASS)
    for sibling in (a, c):
        (ref,) = _ref_records(registry, sibling)
        assert ref["executor_ref"].startswith("ref-")
    assert {t for t, _, _ in executor.spawns} == {a.id, c.id}


async def test_a_tick_dying_of_an_outage_leaves_its_build_flagged(
    default_in_memory_fs_target: Target,
):
    task = SyncOnlyTask(name=f"t-{new_id()}")

    class Unavailable(InMemoryRegistry):
        def member_start(self, plan_id, task_id, **kwargs):
            raise _outage()

    registry = Unavailable()
    build_id, _ = await _plan(registry, [task])
    with pytest.raises(APIError):
        await tick(
            registry, build_id, FakeDetachedExecutor(registry=registry), ONE_PASS
        )
    # Flagged without a hand-out: a successor spawned now would meet the
    # same outage.
    assert {"build_id": build_id, "can_spawn": False} in registry.calls_to(
        "build_notify"
    )
    assert registry.builds[build_id].needs_tick


async def test_a_refusal_is_not_an_outage(default_in_memory_fs_target: Target):
    assert not _tick._is_registry_outage(
        APIError("no", status_code=409, payload={"code": "x"})
    )
    assert _tick._is_registry_outage(RateLimitError(retry_after=1))
    assert _tick._is_registry_outage(_outage())
    # A creation quota is a 429 too, but a refusal for the day: flagging
    # for it would only repeat it.
    assert not _tick._is_registry_outage(QuotaExceededError("creation quota"))


async def test_an_outage_exit_spawns_nothing(default_in_memory_fs_target: Target):
    """Neither the drain nor the hand-off runs on the way out of an
    outage: a tick spawned now would meet the same outage."""
    task = SyncOnlyTask(name=f"t-{new_id()}")
    spawned: list = []

    class Unavailable(InMemoryRegistry):
        def member_start(self, plan_id, task_id, **kwargs):
            # A worker's wake-up lands mid-pass, as it would in production.
            self.build_notify(build_id)
            raise _outage()

    registry = Unavailable()
    build_id, _ = await _plan(registry, [task])
    config = TickConfig(
        linger_seconds=0,
        poll_interval_seconds=0.01,
        spawn_tick=lambda build, app: spawned.append(build),
    )
    with pytest.raises(APIError):
        await tick(registry, build_id, FakeDetachedExecutor(registry=registry), config)
    assert spawned == []
    assert registry.builds[build_id].needs_tick


async def test_an_outage_acquiring_the_lease_is_flagged(
    default_in_memory_fs_target: Target,
):
    """Copilot on #506: the acquire is a registry call too."""
    task = SyncOnlyTask(name=f"t-{new_id()}")

    class AcquireFails(InMemoryRegistry):
        def scheduler_lease_acquire(self, build_id, **kwargs):
            raise _outage()

    registry = AcquireFails()
    build_id, _ = await _plan(registry, [task])
    with pytest.raises(APIError):
        await tick(
            registry, build_id, FakeDetachedExecutor(registry=registry), ONE_PASS
        )
    assert {"build_id": build_id, "can_spawn": False} in registry.calls_to(
        "build_notify"
    )


async def test_a_lease_lapsed_on_unanswered_renewals_is_flagged(
    default_in_memory_fs_target: Target, monkeypatch
):
    """The same outage seen from the lease's side: renewals that go
    unanswered end the tick ``lease_lost`` without an exception."""
    from stardag.build._reactive import _lease

    task = SyncOnlyTask(name=f"t-{new_id()}")
    monkeypatch.setattr(_lease, "_LEASE_TTL_SECONDS", 0.2)
    monkeypatch.setattr(_lease, "_LEASE_RENEW_INTERVAL_SECONDS", 0.05)

    class RenewalsFail(InMemoryRegistry):
        def scheduler_lease_renew(self, build_id, **kwargs):
            raise _outage()

    registry = RenewalsFail()
    build_id, _ = await _plan(registry, [task])
    executor = _SlowSpawns(registry=registry)

    async def slow_spawn(task, *, execution_id):
        await asyncio.sleep(0.5)
        return await FakeDetachedExecutor.submit_detached(
            executor, task, execution_id=execution_id
        )

    executor.submit_detached = slow_spawn  # type: ignore[method-assign]
    summary = await tick(registry, build_id, executor, ONE_PASS)
    assert summary.outcome == "lease_lost"
    assert {"build_id": build_id, "can_spawn": False} in registry.calls_to(
        "build_notify"
    )


async def test_a_ref_the_registry_did_not_answer_for_is_not_an_orphan(
    default_in_memory_fs_target: Target,
):
    """The worker records its own ref when it starts; cancelling it, or
    failing the pass, would be the wrong answer to a registry that was
    merely unavailable for a moment."""
    task = SyncOnlyTask(name=f"t-{new_id()}")

    class RefRecordFails(InMemoryRegistry):
        def member_start(self, plan_id, task_id, **kwargs):
            if not kwargs.get("claim", True):
                raise _outage()
            return super().member_start(plan_id, task_id, **kwargs)

    registry = RefRecordFails()
    build_id, _ = await _plan(registry, [task])
    executor = FakeDetachedExecutor(registry=registry)
    summary = await tick(registry, build_id, executor, ONE_PASS)
    assert summary.spawned == 1
    assert executor.cancel_detached_calls == []


async def test_a_spawn_failure_that_cannot_be_recorded_does_not_fail_the_pass(
    default_in_memory_fs_target: Target,
):
    task = SyncOnlyTask(name=f"t-{new_id()}")

    class FailRecordFails(InMemoryRegistry):
        def member_fail(self, *args, **kwargs):
            raise _outage()

    registry = FailRecordFails()
    build_id, _ = await _plan(registry, [task])
    executor = FakeDetachedExecutor(registry=registry, spawn_error=RuntimeError("no"))
    summary = await tick(registry, build_id, executor, ONE_PASS)
    assert summary.spawn_failed == 1


class TestAdmission:
    @pytest.fixture
    def counter(self, monkeypatch) -> list[int]:
        count = [0]
        monkeypatch.setattr(_frontier_actions, "_rate_limited_count", lambda: count[0])
        return count

    def test_the_cap_applies_alone_until_a_rate_limit_is_met(self, counter):
        admission = Admission()
        assert admission.apply(64) == 64
        admission.after_pass()
        assert admission.apply(64) == 64

    def test_halved_on_a_rate_limit_and_grown_back_without_one(self, counter):
        admission = Admission()
        admission.apply(64)
        counter[0] += 3
        admission.after_pass()
        assert admission.apply(64) == 32
        counter[0] += 1
        admission.after_pass()
        assert admission.apply(64) == 16
        admission.after_pass()  # no new 429: + 64 // 8
        assert admission.apply(64) == 24
        for _ in range(5):
            admission.after_pass()
        assert admission.limit is None
        assert admission.apply(64) == 64

    def test_halved_from_what_the_shrunken_cap_admits(self, counter):
        """Copilot on #506: a stored limit above a cap that has since
        shrunk halves from the cap, not from the stale limit."""
        admission = Admission()
        admission.apply(512)
        counter[0] += 1
        admission.after_pass()
        assert admission.apply(50) == 50  # stored 256, capped
        counter[0] += 1
        admission.after_pass()
        assert admission.apply(50) == 25

    def test_never_below_one(self, counter):
        admission = Admission()
        admission.apply(2)
        for _ in range(5):
            counter[0] += 1
            admission.after_pass()
        assert admission.apply(2) == 1

    async def test_a_throttled_pass_spawns_fewer(
        self, counter, monkeypatch, default_in_memory_fs_target: Target
    ):
        """Every pass meets a rate limit: the first spawns the cap, each
        later one half the previous."""

        class Throttled(Admission):
            def after_pass(self) -> None:
                counter[0] += 1
                super().after_pass()

        monkeypatch.setattr(_tick, "Admission", Throttled)
        tasks: list[BaseTask] = [
            SyncOnlyTask(name=f"t{i}-{new_id()}") for i in range(7)
        ]
        registry = InMemoryRegistry()
        build_id, _ = await _plan(registry, tasks)
        executor = FakeDetachedExecutor(registry=registry)
        config = TickConfig(
            linger_seconds=0, poll_interval_seconds=0.01, max_spawns_per_tick=4
        )
        per_pass: list[int] = []

        class Recording(Throttled):
            def after_pass(self) -> None:
                per_pass.append(len(executor.spawns) - sum(per_pass))
                super().after_pass()

        monkeypatch.setattr(_tick, "Admission", Recording)
        summary = await tick(registry, build_id, executor, config)
        assert summary.spawned == 7
        # The cap alone would spawn 4 then 3; admission halves every pass.
        assert per_pass[:3] == [4, 2, 1]

    async def test_a_denied_claim_gives_its_place_to_the_next_member(
        self, counter, monkeypatch, default_in_memory_fs_target: Target
    ):
        """Copilot on #506: admission counts granted claims, so a denial at
        the head of a throttled pass does not leave it idle."""
        first, second = (SyncOnlyTask(name=f"{n}-{new_id()}") for n in "ab")

        class DeniesFirst(InMemoryRegistry):
            def member_start(self, plan_id, task_id, **kwargs):
                if kwargs.get("claim", True) and task_id == str(first.id):
                    raise refuse("concurrency_limit_reached")
                return super().member_start(plan_id, task_id, **kwargs)

        class OneAtATime(Admission):
            def __init__(self) -> None:
                super().__init__()
                self.limit = 1

        monkeypatch.setattr(_tick, "Admission", OneAtATime)
        registry = DeniesFirst()
        build_id, _ = await _plan(registry, [first, second])
        executor = FakeDetachedExecutor(registry=registry)
        summary = await tick(registry, build_id, executor, ONE_PASS)
        assert [t for t, _, _ in executor.spawns] == [second.id]
        assert summary.limit_denied >= 1
