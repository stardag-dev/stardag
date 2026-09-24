"""Live e2e: orchestrator crash -> the next build waits on the claim, and the
task is not restarted.

1. Phase A (subprocess): a build claims a long-running task and spawns it
   on Modal via the detached ``ModalTaskExecutor``; its registry records
   the execution id and the executor ref (function call id) of the
   holder's non-claiming start to a file. The subprocess is then SIGKILLed
   -- a hard orchestrator crash, no cancel or cleanup runs.
2. Phase B (this process): a registry that holds that claim (seeded from
   the file, as the real registry would still hold it) and a second build
   of the same task. Its claiming start is denied ``task_already_running``;
   it waits on the claim and, once the original worker's output lands
   (and its completion report, relayed here -- see ``RelayingRegistry``),
   finds the task completed instead of spawning again.

The task saves the Modal function call id it ran under; asserting it equals
the ref recorded *before the crash* proves the original invocation produced
the output.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

VOLUME_NAME = "stardag-testing"
ROOT_DEFAULT = "stardag/root/default"
TEST_APP_NAME = "stardag-testing-app"

try:
    import modal  # noqa: F401

    from stardag.testing.modal import live_modal_guard

    live_modal_guard(VOLUME_NAME)

    from stardag.build import BuildExitStatus, build
    from stardag.integration.modal._executor import ModalTaskExecutor
    from stardag.integration.modal._metadata import MODAL_EXECUTOR_NAME
    from stardag.build import ClaimConfig
    from stardag.build._registration import new_id, registration_item
    from stardag.testing import InMemoryRegistry
    from stardag.testing.modal._tasks import SleepAndSaveCallId

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

pytestmark = pytest.mark.modal_live

MODAL_TARGET_ROOT = f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"


@pytest.fixture(autouse=True)
def modal_target_factory():
    """Point the default target root at the test Modal volume.

    Explicit provider override rather than env vars: the local stardag
    profile may define its own target roots (e.g. s3://...) which would
    otherwise win, making completeness checks hit the wrong backend.
    """
    from stardag.target._factory import TargetFactory, target_factory_provider

    with target_factory_provider.override(
        TargetFactory(target_roots={"default": MODAL_TARGET_ROOT})
    ):
        yield


@pytest.fixture(scope="module", autouse=True)
def ensure_app_deployed():
    """Deploy the shared test Modal app (same app as test__app.py)."""
    result = subprocess.run(
        ["modal", "deploy", str(Path(__file__).parent / "test__app.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to deploy Modal app:\n{result.stderr}\n{result.stdout}")
    yield


# Phase A orchestrator, run as a subprocess so it can be SIGKILLed mid-build.
# Self-contained: only imports installed stardag modules (no test imports).
_ORCHESTRATOR_SCRIPT = """
import asyncio
import json
import sys

from stardag.build import build_aio
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.testing import InMemoryRegistry
from stardag.testing.modal._tasks import SleepAndSaveCallId


class FileRefRegistry(InMemoryRegistry):
    '''Records the holder's non-claiming start (its executor ref) to a file.'''

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def member_start(self, plan_id, task_id, **kwargs):
        result = super().member_start(plan_id, task_id, **kwargs)
        if not kwargs.get("claim", True) and kwargs.get("executor_ref"):
            with open(self.path, "w") as f:
                json.dump(
                    {
                        "task_id": task_id,
                        "execution_id": str(kwargs["execution_id"]),
                        "executor": kwargs.get("executor"),
                        "executor_ref": kwargs["executor_ref"],
                    },
                    f,
                )
        return result


async def main():
    ref_file, sleep_seconds, salt = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    target_root = sys.argv[4]

    from stardag.target._factory import TargetFactory, target_factory_provider

    target_factory_provider.set(TargetFactory(target_roots={"default": target_root}))

    task = SleepAndSaveCallId(sleep_seconds=sleep_seconds, salt=salt)
    executor = ModalTaskExecutor(
        modal_app_name="stardag-testing-app",
        worker_selector=lambda t: "default",
        worker_reports_lifecycle=False,
    )
    registry = FileRefRegistry(ref_file)
    registry.add_deployment(app_name="stardag-testing-app")
    await build_aio([task], task_executor=executor, registry=registry)


asyncio.run(main())
"""


def _run_phase_a_and_crash(tmp_path: Path, sleep_seconds: float, salt: str) -> dict:
    """Start the orchestrator subprocess, wait for the spawn ref, SIGKILL it."""
    ref_file = tmp_path / "ref.json"
    script = tmp_path / "orchestrator.py"
    script.write_text(_ORCHESTRATOR_SCRIPT)
    log_file = tmp_path / "orchestrator.log"

    with open(log_file, "w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(ref_file),
                str(sleep_seconds),
                salt,
                MODAL_TARGET_ROOT,
            ],
            env={**os.environ},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.time() + 120
        while not (ref_file.exists() and ref_file.stat().st_size > 0):
            if proc.poll() is not None:
                pytest.fail(
                    "Phase-A orchestrator exited before spawning the task:\n"
                    + log_file.read_text()
                )
            if time.time() > deadline:
                pytest.fail(
                    "Timed out waiting for the executor ref:\n" + log_file.read_text()
                )
            time.sleep(0.5)
        ref_info = json.loads(ref_file.read_text())
    finally:
        # Hard crash — SIGKILL, so no cancel()/teardown runs in the
        # orchestrator and the detached worker keeps running.
        proc.kill()
        proc.wait(timeout=10)
    return ref_info


def test_crash_resume_reattaches_without_restarting_task(tmp_path):
    salt = uuid.uuid4().hex  # unique task id/output per test run
    sleep_seconds = 40.0
    task = SleepAndSaveCallId(sleep_seconds=sleep_seconds, salt=salt)
    assert not task.complete(), "fresh salt must yield an incomplete task"

    ref_info = _run_phase_a_and_crash(tmp_path, sleep_seconds, salt)
    assert ref_info["task_id"] == str(task.id)
    assert ref_info["executor"] == MODAL_EXECUTOR_NAME
    original_ref = ref_info["executor_ref"]
    assert original_ref and original_ref.startswith("fc-")

    # Phase B -- the registry still holds the pre-crash execution's claim.
    # The crashed build's plan is as its static phase left it: the task
    # registered *expanded* (it has no upstreams) and the plan sealed. A
    # claiming start re-checks upstream completion under its lock (S39), so
    # an unexpanded member -- upstreams unknown -- would be refused
    # ``upstream_incomplete`` rather than granted.
    original_execution = uuid.UUID(ref_info["execution_id"])

    class RelayingRegistry(InMemoryRegistry):
        """Relays the original worker's own completion report.

        The detached worker runs with ``worker_reports_lifecycle=False``
        because it cannot reach this in-process registry; a real one reports
        its completion itself. v2 records a completion from the holder's
        report while its claim is live -- an observation is refused against
        a live claim ("its holder reports") -- so without the relay the
        second build would see the output land and still be refused
        ``plan_incomplete`` at ``/complete``. Relayed at the second build's
        next claim attempt once the output exists, in this thread (the fake
        is not thread-safe).
        """

        crashed_plan_id: uuid.UUID | None = None

        def member_start(self, plan_id, task_id, **kwargs):
            if (
                kwargs.get("claim", True)
                and self.crashed_plan_id is not None
                and self.tasks[task_id].status == "running"
                and task.complete()
            ):
                self.member_complete(
                    self.crashed_plan_id, task_id, execution_id=original_execution
                )
            return super().member_start(plan_id, task_id, **kwargs)

    registry = RelayingRegistry()
    deployment = registry.add_deployment(app_name=TEST_APP_NAME)
    crashed_build = registry.build_create(root_task_ids=[str(task.id)]).id
    observed_at = datetime.now(timezone.utc)
    plan = registry.plan_create(
        crashed_build,
        plan_id=new_id(),
        deployment_id=deployment,
        settings={},
        # Roots are admitted first and unexpanded ...
        roots=[
            registration_item(
                task,
                declared_upstreams=None,
                observed_complete=False,
                observed_at=observed_at,
            )
        ],
    )
    # ... and the walk's chunk expands them.
    registry.plan_register_members(
        plan.id,
        [
            registration_item(
                task,
                declared_upstreams=[],
                observed_complete=False,
                observed_at=observed_at,
            )
        ],
    )
    registry.plan_seal(plan.id)
    registry.member_start(
        plan.id,
        str(task.id),
        execution_id=original_execution,
        claim_ttl_seconds=3600,
    )
    registry.crashed_plan_id = plan.id

    class CountingExecutor(ModalTaskExecutor):
        spawns = 0

        async def submit_detached(self, task, *, execution_id):
            CountingExecutor.spawns += 1
            return await super().submit_detached(task, execution_id=execution_id)

    executor = CountingExecutor(
        modal_app_name=TEST_APP_NAME,
        worker_selector=lambda t: "default",
        worker_reports_lifecycle=False,
    )
    summary = build(
        [task],
        task_executor=executor,
        registry=registry,
        claim_config=ClaimConfig(
            wait_timeout_seconds=180,
            wait_initial_interval_seconds=1.0,
            wait_max_interval_seconds=5.0,
        ),
    )

    assert summary.status == BuildExitStatus.SUCCESS
    assert CountingExecutor.spawns == 0
    assert task.complete()
    result = task.load()
    assert result["salt"] == salt
    # The crux: the output was produced by the ORIGINAL (pre-crash) worker
    # invocation -- the second build waited instead of re-executing.
    assert result["call_id"] == original_ref
