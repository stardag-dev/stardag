"""Live e2e: two builds race one task — the execution claim ensures it runs
exactly once on Modal.

Both builds run as concurrent ``build_aio`` coroutines in one event loop,
sharing one in-memory registry (``stardag.testing.InMemoryRegistry``, which
follows the server's claim seams). Build B starts after the winner claimed,
so its claiming start is denied ``task_already_running``; it waits on the
claim and, once the winner completes, observes the completion instead of
spawning a duplicate.

Ground truth assertions: exactly one worker spawn across both builds, and
the task's saved function call id equals the single recorded executor ref.
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
    from stardag.testing import InMemoryRegistry
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


class CountingModalExecutor(ModalTaskExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spawn_count = 0

    async def submit_detached(self, task, *, execution_id):
        self.spawn_count += 1
        return await super().submit_detached(task, execution_id=execution_id)


def test_racing_builds_execute_task_exactly_once():
    salt = uuid_module.uuid4().hex
    task = SleepAndSaveCallId(sleep_seconds=25.0, salt=salt)
    assert not task.complete()

    registry = InMemoryRegistry()
    registry.add_deployment(app_name=TEST_APP_NAME)
    # The test app's workers have no registry of their own: the engines
    # report the lifecycle.
    executor_a = CountingModalExecutor(
        modal_app_name=TEST_APP_NAME,
        worker_selector=lambda t: "default",
        worker_reports_lifecycle=False,
    )
    executor_b = CountingModalExecutor(
        modal_app_name=TEST_APP_NAME,
        worker_selector=lambda t: "default",
        worker_reports_lifecycle=False,
    )
    claim_config = ClaimConfig(
        wait_timeout_seconds=120,
        wait_initial_interval_seconds=0.5,
        wait_max_interval_seconds=2.0,
    )

    async def build_b_delayed():
        # Give the winner time to claim and spawn, so the loser's claiming
        # start is denied (the interesting path).
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
    # The loser was denied the claim rather than running a second copy.
    denials = [
        c for c in registry.calls_to("member_start", task_id=str(task.id)) if c["claim"]
    ]
    assert len(denials) >= 2
    # Ground truth: the single recorded ref produced the output.
    assert task.complete()
    (execution,) = [e for e in registry.executions.values() if e.executor_ref]
    recorded_executor, recorded_ref = execution.executor, execution.executor_ref
    assert recorded_executor == MODAL_EXECUTOR_NAME
    result = task.load()
    assert result["salt"] == salt
    assert result["call_id"] == recorded_ref
