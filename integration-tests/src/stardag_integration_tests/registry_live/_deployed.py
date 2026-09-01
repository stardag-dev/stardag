"""Calling a deployed app's own functions.

Almost nothing here needs this. The scenarios trigger a build and then
watch, because what they are testing is precisely that nobody had to
intervene: the registry flags a wake candidate on *every* task status
transition, so a build that needs another tick gets one without being
asked. A test that drove ticks by hand would be racing those, and would as
often as not be told the scheduler lease is already held -- a tick that
reports nothing about the situation it was called to judge.

The exception is the watchdog sweep, which is a deployed function with no
other way in. It never fires on its own here: both apps deploy with the
periodic schedule off, deliberately, since a sweep running in the
background would eventually complete every build in the tier and none of
the other scenarios would prove anything.
"""

from __future__ import annotations

import time


def deployed_function(app_name: str, function_name: str, modal_environment: str):
    """A handle on one function of a deployed app.

    ``environment_name`` is passed explicitly rather than left to the
    ambient ``MODAL_ENVIRONMENT``. This tier's whole safety story is that
    everything it touches lives in the run's own environment, and a lookup
    that silently resolved elsewhere would reach into a workspace that also
    holds standing deployments.
    """
    import modal

    return modal.Function.from_name(
        app_name, function_name, environment_name=modal_environment
    )


def run_watchdog_sweep(*, app_name: str, modal_environment: str) -> float:
    """Drive one watchdog sweep. Returns how long the call took, in seconds.

    The duration is an observable in its own right, not instrumentation.
    The sweep's contract is that it *dispatches* -- it lists the app's
    running builds, spawns a tick for each, and returns -- so its cost has
    to be independent of how many builds it found. An implementation that
    ran the tick bodies inline would be correct in every other respect and
    would show up here, as a call that takes as long as the work does.
    """
    function = deployed_function(app_name, "tick_watchdog", modal_environment)
    started = time.monotonic()
    function.remote()
    return time.monotonic() - started
