"""Live regression tests for the Modal semantics stardag's execution relies on.

The (planned) detached-execution model — spawning workers with
``Function.spawn`` and tracking them via function call ids — depends on
specific Modal platform behaviors. These tests pin them against a real
workspace so a Modal-side change is caught here rather than in production
builds:

1. A spawned call on a deployed app **survives the exit of the spawning
   process** (detached execution).
2. ``FunctionCall.from_id`` re-attaches to a call from a different process,
   and ``get(timeout=0)`` is a usable non-blocking status poll.
3. Modal-level retries of a spawned call **preserve the function call id**
   (so the id can serve as a stable execution-claim owner).
4. ``FunctionCall.cancel()`` on a re-attached handle terminates a running
   call, and ``get()`` then raises.

The probe app uses ``serialized=True`` functions (like ``StardagApp``) so no
source files or extra packages are needed in the container image.
"""

import subprocess
import sys
import time
import uuid

import pytest

try:
    import modal
    from modal.exception import RemoteError

    from stardag.testing.modal import live_modal_guard

    live_modal_guard()

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

pytestmark = pytest.mark.modal_live

PROBE_APP_NAME = "stardag-testing-probe"
ATTEMPTS_DICT_NAME = "stardag-testing-probe-attempts"


def _build_probe_app() -> modal.App:
    """Build the probe app with functions defined as *closures*.

    With ``serialized=True``, cloudpickle serializes module-level functions
    **by reference** (module path + name) — the container would then try to
    import this test module (pytest, stardag, ...) and crash-loop. Closures
    are pickled by value, so the container needs nothing but ``modal``
    itself. This mirrors why ``StardagApp.finalize()`` registers nested
    wrapper functions instead of module-level callables.
    """
    app = modal.App(PROBE_APP_NAME)
    image = modal.Image.debian_slim()

    def probe_slow_ok(seconds: int) -> str:
        import time as _time

        import modal as _modal

        call_id = _modal.current_function_call_id()
        _time.sleep(seconds)
        return f"done after {seconds}s inside call_id={call_id}"

    def probe_flaky(key: str, dict_name: str) -> dict:
        """Fail on the first attempt, succeed on the second, recording the
        function call id seen by each attempt."""
        import modal as _modal

        call_id = _modal.current_function_call_id()
        attempts_dict = _modal.Dict.from_name(dict_name, create_if_missing=True)
        attempts = attempts_dict.get(key, []) + [call_id]
        attempts_dict[key] = attempts
        if len(attempts) < 2:
            raise RuntimeError(f"deliberate failure on attempt {len(attempts)}")
        return {"attempt_call_ids": attempts, "final_call_id": call_id}

    app.function(image=image, timeout=300, name="probe_slow_ok", serialized=True)(
        probe_slow_ok
    )
    app.function(
        image=image, timeout=120, retries=2, name="probe_flaky", serialized=True
    )(probe_flaky)
    return app


@pytest.fixture(scope="module")
def deployed_probe_app() -> str:
    """Deploy the probe app (idempotent, ~2s) and return its name."""
    from modal.runner import deploy_app

    deploy_app(_build_probe_app(), name=PROBE_APP_NAME)
    return PROBE_APP_NAME


def test_spawned_call_survives_caller_exit_and_reattaches(deployed_probe_app):
    """Spawn in a subprocess that exits immediately; re-attach here by id.

    Also verifies ``get(timeout=0)`` raises TimeoutError while the call is
    still running (the non-blocking poll a scheduler tick would use).
    """
    code = (
        "import modal; "
        f"fn = modal.Function.from_name('{deployed_probe_app}', 'probe_slow_ok'); "
        "print(fn.spawn(20).object_id)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    call_id = proc.stdout.strip().splitlines()[-1]
    assert call_id.startswith("fc-")

    # The spawning process is gone; re-attach from this process.
    fc = modal.FunctionCall.from_id(call_id)

    with pytest.raises(TimeoutError):
        fc.get(timeout=0)  # still running — non-blocking poll

    result = fc.get(timeout=180)
    assert f"call_id={call_id}" in result


def test_retries_preserve_function_call_id(deployed_probe_app):
    """All retry attempts of a spawned call see the same function call id.

    The detached-execution design keys execution claims on the call id;
    retries must be re-entrant on the same claim.
    """
    key = f"probe-{uuid.uuid4().hex[:8]}"
    fn = modal.Function.from_name(deployed_probe_app, "probe_flaky")
    fc = fn.spawn(key, ATTEMPTS_DICT_NAME)

    result = fc.get(timeout=180)

    assert len(result["attempt_call_ids"]) == 2  # failed once, then succeeded
    assert all(a == fc.object_id for a in result["attempt_call_ids"])


def test_cancel_running_spawned_call(deployed_probe_app):
    """A re-attached handle can cancel a running call; get() then raises."""
    fn = modal.Function.from_name(deployed_probe_app, "probe_slow_ok")
    fc = fn.spawn(180)
    time.sleep(5)  # let it get scheduled/started

    reattached = modal.FunctionCall.from_id(fc.object_id)
    reattached.cancel()

    with pytest.raises(RemoteError):
        reattached.get(timeout=60)
