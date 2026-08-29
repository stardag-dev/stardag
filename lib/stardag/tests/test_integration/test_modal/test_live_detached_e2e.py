"""Live e2e: orchestrator crash → resumed build re-attaches, task not restarted.

The headline scenario of detached execution:

1. Phase A (subprocess): a build spawns a long-running task on Modal via the
   detached ``ModalTaskExecutor``; a file-backed registry stub records the
   executor ref (function call id) at TASK_STARTED. The subprocess is then
   SIGKILLed — a hard orchestrator crash, no cancel/cleanup runs.
2. Phase B (this process): a "resumed" build whose registry reports the task
   RUNNING with the recorded ref (exactly what the API registry does after
   this change). The engine re-attaches instead of re-executing and awaits
   the original worker.

The task saves the Modal function call id it ran under; asserting it equals
the ref recorded *before the crash* proves the original invocation produced
the output — i.e. the task was not restarted.
"""

import json
import os
import subprocess
import sys
import time
import uuid
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
    from stardag.registry import NoOpRegistry, RegisteredTaskInfo
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
from stardag.registry import NoOpRegistry
from stardag.testing.modal._tasks import SleepAndSaveCallId


class FileRefRegistry(NoOpRegistry):
    '''Records each TASK_STARTED executor ref to a JSON file.'''

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    async def task_start_aio(
        self, build_id, task, executor=None, executor_ref=None, **kwargs
    ):
        with open(self.path, "w") as f:
            json.dump(
                {
                    "task_id": str(task.id),
                    "executor": executor,
                    "executor_ref": executor_ref,
                },
                f,
            )


async def main():
    ref_file, sleep_seconds, salt = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    target_root = sys.argv[4]

    from stardag.target._factory import TargetFactory, target_factory_provider

    target_factory_provider.set(TargetFactory(target_roots={"default": target_root}))

    task = SleepAndSaveCallId(sleep_seconds=sleep_seconds, salt=salt)
    executor = ModalTaskExecutor(
        modal_app_name="stardag-testing-app",
        worker_selector=lambda t: "default",
    )
    await build_aio(
        [task], task_executor=executor, registry=FileRefRegistry(ref_file)
    )


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

    # Phase B — "resumed" orchestrator. The registry stub reports the task
    # as RUNNING with the pre-crash ref, as the API registry would.
    class ReattachRegistry(NoOpRegistry):
        async def task_register_bulk_aio(self, build_id, tasks, *, limit_keys=None):
            return [
                RegisteredTaskInfo(
                    task_id=str(t.id),
                    latest_status="running",
                    latest_executor=MODAL_EXECUTOR_NAME,
                    latest_executor_ref=original_ref,
                )
                for t in tasks
            ]

    executor = ModalTaskExecutor(
        modal_app_name=TEST_APP_NAME,
        worker_selector=lambda t: "default",
    )
    summary = build([task], task_executor=executor, registry=ReattachRegistry())

    assert summary.status == BuildExitStatus.SUCCESS
    assert task.complete()
    result = task.load()
    assert result["salt"] == salt
    # The crux: the output was produced by the ORIGINAL (pre-crash) worker
    # invocation — the resumed build re-attached instead of re-executing.
    assert result["call_id"] == original_ref
