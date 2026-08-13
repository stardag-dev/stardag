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

# Timeout of the interruption probes. Short enough to keep the suite quick,
# long enough that container startup cannot be mistaken for it.
PROBE_TIMEOUT_SECONDS = 20


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

    def probe_observe_interruption(key: str, dict_name: str, sleep_for: int) -> str:
        """Record whatever BaseException ends this call, and when.

        ``sleep_for=0`` means "sleep past the function timeout", which is
        how the timeout case is provoked; a large value plus an external
        ``cancel()`` provokes the cancellation case. The two write the same
        shape so a test can compare them.
        """
        import time as _time

        import modal as _modal

        observations = _modal.Dict.from_name(dict_name, create_if_missing=True)
        # Announce that user code is actually running, so a test that means
        # to interrupt *this* can wait for it. Without the handshake a
        # cancel issued on a wall-clock guess lands while the container is
        # still cold-starting, the input is dropped before it runs, and
        # nothing is ever observed.
        observations[f"{key}/running"] = True
        started = _time.monotonic()
        try:
            _time.sleep(sleep_for or 3600)
        except BaseException as e:
            observations[key] = {
                "type": type(e).__name__,
                "module": type(e).__module__,
                "message": str(e),
                # True when it does NOT derive from Exception — i.e. a
                # task's own `except Exception` would not catch it.
                "is_base_exception_only": not isinstance(e, Exception),
                "at": round(_time.monotonic() - started, 3),
            }
            raise
        return "no interruption arrived"

    def probe_escape_base_exception(key: str, dict_name: str) -> dict:
        """Let a KeyboardInterrupt escape, and count how often we are run.

        Guarded: succeeds on the third attempt so a platform that restarts
        indefinitely cannot loop.
        """
        import time as _time

        import modal as _modal

        observations = _modal.Dict.from_name(dict_name, create_if_missing=True)
        state = observations.get(key) or {"attempts": 0, "call_ids": []}
        state["attempts"] += 1
        state["call_ids"].append(_modal.current_function_call_id())
        observations[key] = state
        if state["attempts"] >= 3:
            return state
        _time.sleep(1)
        raise KeyboardInterrupt("simulated platform interrupt")

    def probe_return_after_timeout(key: str, dict_name: str, sleep_for: int) -> str:
        """Catch the timeout signal, 'checkpoint', and return normally."""
        import time as _time

        import modal as _modal

        observations = _modal.Dict.from_name(dict_name, create_if_missing=True)
        state = observations.get(key) or {"attempts": 0, "returned_cleanly": False}
        state["attempts"] += 1
        observations[key] = state
        try:
            _time.sleep(sleep_for or 3600)
        except BaseException:
            _time.sleep(2)  # "write a checkpoint"
            state["returned_cleanly"] = True
            observations[key] = state
            return "checkpointed and returned"
        return "no interruption arrived"

    app.function(image=image, timeout=300, name="probe_slow_ok", serialized=True)(
        probe_slow_ok
    )
    app.function(
        image=image, timeout=120, retries=2, name="probe_flaky", serialized=True
    )(probe_flaky)
    app.function(
        image=image,
        timeout=PROBE_TIMEOUT_SECONDS,
        retries=0,
        name="probe_observe_interruption",
        serialized=True,
    )(probe_observe_interruption)
    app.function(
        image=image,
        timeout=120,
        retries=0,
        name="probe_escape_base_exception",
        serialized=True,
    )(probe_escape_base_exception)
    app.function(
        image=image,
        timeout=PROBE_TIMEOUT_SECONDS,
        retries=0,
        name="probe_return_after_timeout",
        serialized=True,
    )(probe_return_after_timeout)
    return app


def _wait_for(predicate, *, timeout: float, interval: float = 1.0) -> None:
    """Poll ``predicate`` until true, or fail the test saying it never was."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def _await_observation(key: str, *, timeout: float = 60.0) -> dict:
    """Read a probe's observation once the container has written it.

    Polled rather than read once: the caller learns the call is over as
    soon as the platform decides, while the container is still unwinding
    its ``except`` block and writing what it saw. Reading immediately is a
    race that fails intermittently and looks like "no exception was
    delivered".
    """
    observations = modal.Dict.from_name(ATTEMPTS_DICT_NAME, create_if_missing=True)
    _wait_for(lambda: observations.get(key) is not None, timeout=timeout)
    return observations[key]


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


# --- Interruption semantics (stardag#245) --------------------------------
#
# Four facts the worker's interruption handling is built on, measured
# 2026-08-12 against modal 1.5.0. Each is a platform behaviour rather than
# something stardag controls, and each would break a different part of
# `_runner._classify_interruption` if Modal changed it.
#
# The grace ladder itself (SIGUSR1, then SIGINT ~30s later, then SIGKILL
# ~30s after that) is deliberately NOT pinned: it is a number Modal may
# retune, and nothing in the SDK depends on its exact value — only on
# there being enough room to write a checkpoint and one HTTP call.


def test_timeout_surfaces_as_input_cancellation_at_the_declared_timeout(
    deployed_probe_app,
):
    """A function timeout IS graceful, and lands when the clock says.

    This is what makes checkpointing on timeout possible at all, and it is
    the whole basis for telling a timeout apart from a cancellation: they
    produce the same exception, so only the timing separates them (see
    ``test_cancel_is_indistinguishable_from_a_timeout`` below).
    """
    key = f"timeout-{uuid.uuid4().hex[:8]}"
    fn = modal.Function.from_name(deployed_probe_app, "probe_observe_interruption")
    fc = fn.spawn(key, ATTEMPTS_DICT_NAME, 0)

    with pytest.raises(Exception):
        fc.get(timeout=180)

    observed = _await_observation(key)
    assert observed["type"] == "InputCancellation"
    assert observed["module"] == "modal.exception"
    # A BaseException, which is why `except Exception` in a task body does
    # not swallow it — and why the runner has to catch BaseException.
    assert observed["is_base_exception_only"] is True
    # Delivered at the declared timeout (PROBE_TIMEOUT_SECONDS), which is
    # the comparison `_classify_interruption` makes. Generous bounds: the
    # assertion is "at the timeout, not at some unrelated moment".
    assert PROBE_TIMEOUT_SECONDS - 3 <= observed["at"] <= PROBE_TIMEOUT_SECONDS + 10


def test_cancel_is_indistinguishable_from_a_timeout(deployed_probe_app):
    """``FunctionCall.cancel()`` produces the identical exception.

    The reason the worker cannot classify on type alone. stardag cancels
    its own workers (FAIL_FAST, UI cancel), so a runner that read every
    ``InputCancellation`` as an interruption would put tasks the build just
    cancelled back into the frontier.
    """
    key = f"cancel-{uuid.uuid4().hex[:8]}"
    fn = modal.Function.from_name(deployed_probe_app, "probe_observe_interruption")
    fc = fn.spawn(key, ATTEMPTS_DICT_NAME, 600)
    observations = modal.Dict.from_name(ATTEMPTS_DICT_NAME, create_if_missing=True)

    # Wait for the container to actually be *in* the sleep before
    # cancelling. Cancelling on a wall-clock guess races the cold start,
    # and a cancel that lands first drops the input without user code ever
    # running — which looks like "no exception was delivered" rather than
    # the platform behaviour under test.
    _wait_for(lambda: observations.get(f"{key}/running") is True, timeout=120)
    modal.FunctionCall.from_id(fc.object_id).cancel()

    with pytest.raises(Exception):
        fc.get(timeout=120)

    observed = _await_observation(key)
    assert observed["type"] == "InputCancellation"
    assert observed["module"] == "modal.exception"
    # Nowhere near a timeout — the only thing that distinguishes the two.
    assert observed["at"] < 60


def test_escaping_base_exception_restarts_the_input(deployed_probe_app):
    """An escaping ``BaseException`` reads to Modal as a crashed container,
    so the input is restarted on the same call id — with ``retries=0``.

    This is why preemption needs no registry event: the backend recovers it
    faster than a scheduler could, keeping the claim and spending no
    attempt. It is also why the runner translates ``sd.ResumableInterruption``
    (an ordinary Exception) into a ``BaseException`` on the way out — an
    ordinary exception is a task failure, and gets no restart.
    """
    key = f"escape-{uuid.uuid4().hex[:8]}"
    fn = modal.Function.from_name(deployed_probe_app, "probe_escape_base_exception")
    fc = fn.spawn(key, ATTEMPTS_DICT_NAME)

    result = fc.get(timeout=300)

    assert result["attempts"] >= 2, "the input was not restarted"
    assert all(call_id == fc.object_id for call_id in result["call_ids"])


def test_after_a_timeout_the_escape_route_no_longer_matters(deployed_probe_app):
    """Catching the timeout and returning cleanly does not rescue the call.

    Modal has already decided: the call resolves ``FunctionTimeoutError``
    whatever the container does next. Two consequences the runner relies
    on — a worker cannot save a timed-out execution from the inside (so a
    registry event is the only recovery), and it may re-raise freely
    without a restart racing the scheduler's respawn.
    """
    key = f"graceful-{uuid.uuid4().hex[:8]}"
    fn = modal.Function.from_name(deployed_probe_app, "probe_return_after_timeout")
    fc = fn.spawn(key, ATTEMPTS_DICT_NAME, 0)

    with pytest.raises(modal.exception.FunctionTimeoutError):
        fc.get(timeout=180)

    observed = modal.Dict.from_name(ATTEMPTS_DICT_NAME, create_if_missing=True)[key]
    assert observed["attempts"] == 1, "a timed-out input must not be restarted"
    # Note what is deliberately NOT asserted: that the container's
    # post-catch bookkeeping landed. It does not, reliably — once the input
    # is cancelled Modal stops accepting side effects from it, so the
    # "returned_cleanly" flag the probe tries to set after catching is
    # usually lost. That is one more reason the registry event has to be
    # the recovery path and the container cannot be trusted to tidy up
    # after a timeout; it does not weaken the two assertions above, which
    # are what the runner depends on.
