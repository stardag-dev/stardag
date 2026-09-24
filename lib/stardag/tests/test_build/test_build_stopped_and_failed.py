"""How the resident engines end a build the registry has stopped, and what
they write when a task fails (the ``resident`` tier, against the fake).

- A terminal build status is sticky (design.md, "The runnable rule"): a
  driver still alive after an operator ``cancel`` is refused its next
  claiming start (``build_not_running``) and stops cleanly — no member
  skipped, no ``build_failed`` — and a lifecycle report it still makes is
  refused ``build_terminal`` and leaves the status as it is.
- A failed build's reason names the first failed task and the members the
  registry found blocked by it.
- Every in-process execution records ``executor``, ``executor_ref`` and
  ``executor_metadata`` on its ledger row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
import typing

import pytest

from stardag import Task, auto_namespace
from stardag.build import (
    BuildExitStatus,
    BuildStopped,
    ClaimConfig,
    FailMode,
    build_aio,
    build_sequential,
    build_sequential_aio,
)
from stardag.build._base import describe_task
from stardag.build._registration import new_id
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import FailingTask, SyncOnlyTask

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]

#: The registry the tasks below reach from inside ``run()``, as an operator
#: would from another process.
_REGISTRY: InMemoryRegistry | None = None


def _cancel_the_build() -> None:
    assert _REGISTRY is not None
    (build_id,) = _REGISTRY.builds
    _REGISTRY.build_cancel(build_id)


class CancelsItsBuild(Task[str]):
    """Runs while an operator cancels its build, then finishes."""

    name: str
    linger_seconds: float = 0.0

    def run(self):
        _cancel_the_build()
        time.sleep(self.linger_seconds)
        self._save("done")


async def _sync_sequential(tasks, **kwargs):
    return await asyncio.to_thread(build_sequential, tasks, **kwargs)


ENGINES = pytest.mark.parametrize(
    "engine",
    [build_aio, build_sequential_aio, _sync_sequential],
    ids=["concurrent", "sequential_aio", "sequential"],
)


@pytest.fixture
def registry() -> typing.Iterator[InMemoryRegistry]:
    global _REGISTRY
    _REGISTRY = InMemoryRegistry()
    try:
        yield _REGISTRY
    finally:
        _REGISTRY = None


def _build_events(registry: InMemoryRegistry) -> list[tuple[str, bool]]:
    return [
        (e.type, e.applied)
        for e in registry.events
        if e.type.startswith("BUILD_") and e.type != "BUILD_STARTED"
    ]


@ENGINES
class TestAStoppedBuild:
    async def test_a_refused_claim_stops_the_driver_cleanly(
        self, engine, registry, default_in_memory_fs_target: Target
    ):
        """The operator cancels while the first task runs; that task
        finishes (its completion is late), the next claim is refused
        ``build_not_running``, and the engine stops: ``STOPPED``, the build
        still CANCELLED, nothing skipped and no failure written."""
        first = CancelsItsBuild(name=f"first-{new_id()}")
        root = SyncOnlyTask(name="root", deps=(first,))
        summary = await engine([root], registry=registry)

        assert summary.status == BuildExitStatus.STOPPED
        assert isinstance(summary.error, BuildStopped)
        assert "no longer running (cancelled)" in str(summary.error)
        assert summary.failed_task is None and summary.task_count.failed == 0
        assert summary.build_id is not None
        assert registry.builds[summary.build_id].status == "cancelled"
        assert not registry.called("build_skip_blocked")
        assert not registry.called("build_fail")
        assert not registry.called("member_skip")
        assert _build_events(registry) == [("BUILD_CANCELLED", True)]
        # The root was never started.
        (claim,) = registry.calls_to("member_start", task_id=root.id)
        assert not any(e.task_id == str(root.id) for e in registry.executions.values())
        assert claim["claim"] is True

    async def test_a_refused_completion_leaves_the_status_standing(
        self, engine, registry, default_in_memory_fs_target: Target
    ):
        """The cancel lands while the last task runs: the engine's
        ``/complete`` is refused ``build_terminal`` and recorded, not
        applied; the build stays CANCELLED and the summary says STOPPED."""
        only = CancelsItsBuild(name=f"only-{new_id()}")
        summary = await engine([only], registry=registry)
        assert summary.status == BuildExitStatus.STOPPED
        assert "already cancelled" in str(summary.error)
        assert registry.builds[summary.build_id].status == "cancelled"
        assert _build_events(registry) == [
            ("BUILD_CANCELLED", True),
            ("BUILD_COMPLETED", False),
        ]
        with pytest.raises(Exception):
            summary.raise_on_failure()


async def test_a_released_claim_is_not_reported_as_taken_over(
    registry, default_in_memory_fs_target: Target, caplog: pytest.LogCaptureFixture
):
    """The renewal refused after the cancel says why: the build released
    the claim — not "taken over by another execution"."""
    task = CancelsItsBuild(name=f"linger-{new_id()}", linger_seconds=0.3)
    config = ClaimConfig(in_process_ttl_seconds=60, renew_interval_seconds=0.05)
    with caplog.at_level(logging.WARNING, logger="stardag.build._session"):
        summary = await build_aio([task], registry=registry, claim_config=config)
    assert summary.status == BuildExitStatus.STOPPED
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "the build is no longer running" in messages
    assert "taken over" not in messages


@ENGINES
class TestAFailedBuildsReason:
    async def test_continue_mode_names_the_failed_task_and_what_it_blocks(
        self, engine, registry, default_in_memory_fs_target: Target
    ):
        """The reason used to read "RuntimeError: Deadlock: N tasks cannot
        proceed" with a wrong count. It names the first failed task and the
        members the registry skipped as blocked by it."""
        bad = FailingTask(error_message=f"boom {new_id()}")
        mid = SyncOnlyTask(name="mid", deps=(bad,))
        root = SyncOnlyTask(name="root", deps=(mid,))
        ok = SyncOnlyTask(name=f"ok-{new_id()}")
        summary = await engine(
            [root, ok], registry=registry, fail_mode=FailMode.CONTINUE
        )
        assert summary.status == BuildExitStatus.FAILURE
        assert summary.failed_task == bad
        reason = registry.builds[summary.build_id].error_message
        assert reason == (
            f"Task {describe_task(bad)} failed: ValueError: {bad.error_message}; "
            "2 downstream member(s) blocked"
        )
        assert "Deadlock" not in reason and "\n" not in reason
        assert ok.complete()

    async def test_fail_fast_can_return_the_summary(
        self, engine, registry, default_in_memory_fs_target: Target
    ):
        bad = FailingTask(error_message=f"boom {new_id()}")
        root = SyncOnlyTask(name="root", deps=(bad,))
        summary = await engine([root], registry=registry, raise_on_failure=False)
        assert summary.status == BuildExitStatus.FAILURE
        assert summary.failed_task == bad and summary.build_id is not None
        assert isinstance(summary.error, ValueError)
        reason = registry.builds[summary.build_id].error_message
        assert reason.startswith(f"Task {describe_task(bad)} failed: ValueError")

    async def test_fail_fast_still_raises_by_default(
        self, engine, registry, default_in_memory_fs_target: Target
    ):
        bad = FailingTask(error_message=f"boom {new_id()}")
        with pytest.raises(ValueError, match="boom"):
            await engine([bad], registry=registry)


@pytest.mark.parametrize(
    "engine, kind",
    [(build_aio, "sync_thread"), (build_sequential_aio, "sequential")],
    ids=["concurrent", "sequential"],
)
async def test_in_process_executions_name_their_executor(
    engine, kind, registry, default_in_memory_fs_target: Target
):
    """The execution row is the one place the executor lives: an
    in-process execution records its kind, ``hostname:pid`` and a small
    metadata dict, as a Modal execution records its call."""
    task = SyncOnlyTask(name=f"local-{new_id()}")
    await engine([task], registry=registry)
    (execution,) = registry.executions.values()
    hostname = socket.gethostname()
    assert execution.executor == kind
    assert execution.executor_ref == f"{hostname}:{os.getpid()}"
    assert execution.executor_metadata == {
        "hostname": hostname,
        "pid": os.getpid(),
        "python_version": platform.python_version(),
    }


@pytest.mark.parametrize("plan_complete", [True, False], ids=["complete", "stalled"])
async def test_a_tick_finding_the_build_cancelled_meanwhile_leaves_it(
    registry, plan_complete: bool
):
    """A tick read a RUNNING frontier, then an operator cancelled: its
    ``/complete`` (or its stall ``/fail``) is refused ``build_terminal`` and
    the tick reports the status that stands instead of raising."""
    from stardag.build._reactive._config import TickConfig, TickSummary
    from stardag.build._reactive._frontier_actions import PassResult
    from stardag.build._reactive._terminal import handle_terminal
    from stardag.registry import BuildFrontier

    build = registry.build_create(root_task_ids=["root"])
    frontier = BuildFrontier(
        build_id=build.id,
        plan_id=new_id(),
        sealed=True,
        plan_complete=plan_complete,
        build_status="running",
    )
    registry.build_cancel(build.id)
    status = await handle_terminal(
        frontier,
        build_id=build.id,
        registry=registry,
        config=TickConfig(),
        summary=TickSummary(outcome="running"),
        pass_result=PassResult(),
    )
    assert status == "cancelled"
    assert registry.builds[build.id].status == "cancelled"


class CancelsThenFails(Task[str]):
    """Fails while an operator cancels its build (the race)."""

    name: str

    def run(self):
        _cancel_the_build()
        raise ValueError("failed during the cancel")


@ENGINES
async def test_a_failure_racing_a_cancel_skips_nothing(
    engine, registry, default_in_memory_fs_target: Target
):
    """The build is cancelled while a task fails: the engine's skip-blocked
    finds a cancelled build (nothing failed, nothing to propagate), its
    ``/fail`` is refused ``build_terminal``, and no member is skipped."""
    bad = CancelsThenFails(name=f"race-{new_id()}")
    root = SyncOnlyTask(name="root", deps=(bad,))
    summary = await engine([root], registry=registry, fail_mode=FailMode.CONTINUE)
    assert summary.status == BuildExitStatus.STOPPED
    assert registry.builds[summary.build_id].status == "cancelled"
    assert registry.status_of(root.id) != "skipped"
    assert not any(e.type == "TASK_SKIPPED" for e in registry.events)
    assert _build_events(registry) == [
        ("BUILD_CANCELLED", True),
        ("BUILD_FAILED", False),
    ]


class YieldsAFailingChild(Task[str]):
    name: str

    def run(self):
        yield FailingTask(error_message=f"child of {self.name}")
        self._save("done")


async def test_sequential_a_failing_dynamic_child_is_not_a_deadlock(
    registry, default_in_memory_fs_target: Target
):
    """A dynamic dependency that fails inside its parent is itself failed:
    the reason names it, not a false ``Deadlock``."""
    parent = YieldsAFailingChild(name=f"parent-{new_id()}")
    child = FailingTask(error_message=f"child of {parent.name}")
    summary = await build_sequential_aio(
        [parent], registry=registry, fail_mode=FailMode.CONTINUE
    )
    assert summary.status == BuildExitStatus.FAILURE
    assert summary.failed_task == child
    assert summary.task_count.failed == 2
    # Run once, inside its parent — not again by the top-level loop.
    assert len(registry.calls_to("member_start", task_id=child.id)) == 1
    reason = registry.builds[summary.build_id].error_message
    assert reason.startswith(f"Task {describe_task(child)} failed: ValueError")
    assert "Deadlock" not in reason
