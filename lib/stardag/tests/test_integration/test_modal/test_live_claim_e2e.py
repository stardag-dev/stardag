"""Live e2e: two builds race one task — the execution claim ensures it runs
exactly once on Modal, with the loser re-attaching to the winner.

Both builds run as concurrent ``build_aio`` coroutines in one event loop,
sharing an in-process claim-arbitrating registry (single-threaded ⇒ the
claim is atomic by construction, mirroring the API's FOR-UPDATE
transaction). Build B starts after the winner's executor ref is recorded,
so its claim denial carries a re-attachable ref — the loser attaches to
the *winner's* real Modal function call instead of spawning a duplicate.

Ground truth assertions: exactly one worker spawn across both builds, and
the task's saved function call id equals the single recorded ref.
"""

import asyncio
import subprocess
import uuid as uuid_module
from pathlib import Path

import pytest

VOLUME_NAME = "stardag-testing"
ROOT_DEFAULT = "stardag/root/default"
TEST_APP_NAME = "stardag-testing-app"

try:
    import modal  # noqa: F401

    from stardag.testing.modal import live_modal_guard

    live_modal_guard(VOLUME_NAME)

    from stardag.build import BuildExitStatus, ClaimConfig, build_aio
    from stardag.integration.modal._executor import ModalTaskExecutor
    from stardag.integration.modal._metadata import MODAL_EXECUTOR_NAME
    from stardag.registry import NoOpRegistry, StartClaimResult
    from stardag.testing.modal._tasks import SleepAndSaveCallId

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

pytestmark = pytest.mark.modal_live

MODAL_TARGET_ROOT = f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"


@pytest.fixture(autouse=True)
def modal_target_factory():
    from stardag.target._factory import TargetFactory, target_factory_provider

    with target_factory_provider.override(
        TargetFactory(target_roots={"default": MODAL_TARGET_ROOT})
    ):
        yield


@pytest.fixture(scope="module", autouse=True)
def ensure_app_deployed():
    result = subprocess.run(
        ["modal", "deploy", str(Path(__file__).parent / "test__app.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to deploy Modal app:\n{result.stderr}\n{result.stdout}")
    yield


class SharedClaimRegistry(NoOpRegistry):
    """In-process registry shared by both racing builds (API claim semantics)."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[str, str] = {}
        self.refs: dict[str, tuple[str | None, str | None]] = {}

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
        *,
        claim=True,
    ) -> StartClaimResult:
        tid = str(task.id)
        # Gated on ``claim``, like the server: an unclaiming start acquires
        # limit slots only and is denied by neither state.
        if claim and self.statuses.get(tid) == "running":
            stored_executor, stored_ref = self.refs.get(tid, (None, None))
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor=stored_executor,
                executor_ref=stored_ref,
            )
        if claim and self.statuses.get(tid) == "completed":
            return StartClaimResult(started=False, denied_reason="already_completed")
        self.statuses[tid] = "running"
        return StartClaimResult(started=True)

    async def task_start_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
    ):
        self.statuses[str(task.id)] = "running"
        if executor_ref is not None:
            self.refs[str(task.id)] = (executor, executor_ref)

    async def task_complete_aio(self, build_id, task):
        self.statuses[str(task.id)] = "completed"

    async def task_fail_aio(self, build_id, task, error_message=None):
        self.statuses[str(task.id)] = "failed"


class CountingModalExecutor(ModalTaskExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spawn_count = 0
        self.reattach_successes = 0

    async def submit_detached(self, task):
        self.spawn_count += 1
        return await super().submit_detached(task)

    async def reattach(self, task, executor, ref):
        handle = await super().reattach(task, executor, ref)
        if handle is not None:
            self.reattach_successes += 1
        return handle


def test_racing_builds_execute_task_exactly_once():
    salt = uuid_module.uuid4().hex
    task = SleepAndSaveCallId(sleep_seconds=25.0, salt=salt)
    assert not task.complete()

    registry = SharedClaimRegistry()
    executor_a = CountingModalExecutor(
        modal_app_name=TEST_APP_NAME, worker_selector=lambda t: "default"
    )
    executor_b = CountingModalExecutor(
        modal_app_name=TEST_APP_NAME, worker_selector=lambda t: "default"
    )
    claim_config = ClaimConfig(
        wait_timeout_seconds=120,
        wait_initial_interval_seconds=0.5,
        wait_max_interval_seconds=2.0,
    )

    async def build_b_delayed():
        # Give the winner time to spawn and record its ref, so the loser's
        # denial carries a re-attachable ref (the interesting path).
        await asyncio.sleep(5)
        return await build_aio(
            [task],
            task_executor=executor_b,
            registry=registry,
            claim_config=claim_config,
        )

    async def race():
        return await asyncio.gather(
            build_aio(
                [task],
                task_executor=executor_a,
                registry=registry,
                claim_config=claim_config,
            ),
            build_b_delayed(),
        )

    summary_a, summary_b = asyncio.run(race())

    assert summary_a.status == BuildExitStatus.SUCCESS
    assert summary_b.status == BuildExitStatus.SUCCESS
    # The crux: exactly ONE worker execution across both builds.
    assert executor_a.spawn_count + executor_b.spawn_count == 1
    # The loser resolved by attaching to the winner's live execution.
    assert executor_a.reattach_successes + executor_b.reattach_successes == 1
    # Ground truth: the single recorded ref produced the output.
    assert task.complete()
    recorded_executor, recorded_ref = registry.refs[str(task.id)]
    assert recorded_executor == MODAL_EXECUTOR_NAME
    result = task.load()
    assert result["salt"] == salt
    assert result["call_id"] == recorded_ref
