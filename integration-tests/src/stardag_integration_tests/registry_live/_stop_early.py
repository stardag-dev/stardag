"""Stop a PR run at its first failure that no retry could rescue (STA-172).

The tier runs in **one slot**, and every queued PR waits behind the run in
it. A scenario that fails for real -- an assertion, an HTTP error status --
has already decided the run: it will end red and CI will not retry it. Yet
the run used to go on until its slowest scenario finished, often ten
minutes and more, holding the slot for a verdict it already had.

So, when enabled (``STOP_EARLY_ENV``, which CI sets on PR runs only), the
first **real** failure stops the run from *starting* anything more:

- scenarios already running finish -- they are paid for, and their logs
  are part of the evidence -- and then the session ends;
- a **retryable** failure changes nothing: the run continues, and CI's
  ``--last-failed`` retry re-runs it exactly as before.

Which failures are which is ``_diagnostics.failure_class``, the decision
the retry markers are written from. It is read here, not restated.

**Why stopping takes two signals under xdist.** ``LoadScheduling`` hands
every worker its items up front -- at least two each, and the tier has
about two per worker, so in practice *everything* is on a worker before
the first scenario ends. Stopping the controller's scheduler therefore
stops nothing: the shutdown it sends a worker is queued *behind* the items
that worker already holds. ``--maxfail`` has the same weakness under
``-n`` for the same reason. What does stop a worker is its own
``session.shouldstop``, which xdist's worker loop checks after every item.
So:

1. The worker classifies its own failure, on the report (the exception
   does not survive the trip to the controller; the classification does,
   as a user property). A worker whose own item failed for real stops
   itself at once.
2. The controller, which sees every worker's reports, records the first
   real failure, stops its scheduler, and drops a **stop file** whose
   path every worker was handed at startup. Each worker checks for it
   after every item and stops before starting the next.

Without xdist (``-n0``) the one process is both, and pytest's own loop
honours ``session.shouldstop``.

**A failure that arrives unclassified counts as real.** That is a crash
report xdist synthesizes for a dead worker, or a classifier that raised.
Failing closed costs at most the retry of a run that also lost a worker;
failing open would let the stop be skipped silently.

The controller writes ``STOPPED_EARLY_MARKER_NAME`` into the diagnostics
directory, naming the scenario and phase, which is what the workflow reads
to skip the retry and to say why the run ended where it did.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from ._diagnostics import (
    CLASSIFICATION_FAILED,
    DIAGNOSTICS_DIR_ENV,
    REAL,
    RETRYABLE,
    failure_class,
)

# Set to ``1`` to enable. CI sets it on PR runs, and only on the first
# attempt: a retry runs only what failed, so it is short, and stopping it
# early could leave a scenario the retry owed a result unrun.
STOP_EARLY_ENV = "STARDAG_REGISTRY_LIVE_STOP_AT_REAL_FAILURE"

# Next to the retry markers, in the diagnostics directory.
STOPPED_EARLY_MARKER_NAME = "stopped-at-real-failure"

# The report's user property carrying the worker's classification.
FAILURE_CLASS_PROPERTY = "registry_live_failure_class"

# The ``workerinput`` key that hands each worker the stop file's path.
_STOP_FILE_KEY = "registry_live_stop_file"

PLUGIN_NAME = "registry-live-stop-early"


def is_enabled() -> bool:
    return os.environ.get(STOP_EARLY_ENV, "").strip().lower() in ("1", "true", "yes")


def register(config: pytest.Config) -> None:
    """Install the plugin in this process, if ``STOP_EARLY_ENV`` asks for it.

    Called from ``pytest_configure``, which runs in the controller and in
    every worker; each needs its half.
    """
    if is_enabled() and not config.pluginmanager.has_plugin(PLUGIN_NAME):
        config.pluginmanager.register(StopAtRealFailure(config), PLUGIN_NAME)


def _describe(report: pytest.TestReport) -> str:
    return f"{report.nodeid} [{report.when}]"


class StopAtRealFailure:
    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self._session: pytest.Session | None = None
        workerinput = getattr(config, "workerinput", None)
        self._is_worker = workerinput is not None
        self._stop_file: Path | None = None
        self._owned_dir: Path | None = None
        if workerinput is not None:
            path = workerinput.get(_STOP_FILE_KEY)
            self._stop_file = Path(path) if path else None
        else:
            # Created by the controller, so it exists before any worker
            # starts, and removed when the controller's session ends.
            self._owned_dir = Path(tempfile.mkdtemp(prefix="registry-live-stop-"))
            self._stop_file = self._owned_dir / "stop"
        self.stopped_at: str | None = None
        self._reported: set[str] = set()

    # -- both sides -----------------------------------------------------

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self._session = session

    def _stop(self, reason: str) -> None:
        """Stop starting items in this process; the current one finishes."""
        session = self._session
        if session is not None and not session.shouldstop:
            session.shouldstop = reason

    # -- worker side (and the only side, without xdist) -----------------

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo[None]):
        report = yield
        if report.failed:
            try:
                error = call.excinfo.value if call.excinfo is not None else None
                kind = failure_class(error)
            except Exception as failure:  # pragma: no cover - fails closed
                print(f"{CLASSIFICATION_FAILED}: {failure!r}", file=sys.stderr)
                kind = REAL
            # A fresh list: pytest hands every phase's report the *item's*
            # list, so appending would tag the teardown report with the
            # call's verdict as well.
            report.user_properties = [
                *report.user_properties,
                (FAILURE_CLASS_PROPERTY, kind),
            ]
        return report

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        """After every item: stop before the next if another worker said so."""
        if self._is_worker and self._stop_file is not None:
            if self._stop_file.exists():
                self._stop(self._stop_file.read_text().strip() or "stopped")

    # -- controller side ------------------------------------------------

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: Any) -> None:  # pytest-xdist hook
        if self._stop_file is not None:
            node.workerinput[_STOP_FILE_KEY] = str(self._stop_file)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._reported.add(report.nodeid)
        if not report.failed or self.stopped_at is not None:
            return
        kind = dict(report.user_properties).get(FAILURE_CLASS_PROPERTY)
        if kind == RETRYABLE:
            return
        # Anything else is real, including no classification at all.
        self.stopped_at = _describe(report)
        reason = f"stopped at the first real failure: {self.stopped_at}"
        if self._is_worker:
            # This worker's own item: stop it now, without waiting on the
            # controller to round-trip the stop file.
            self._stop(reason)
            return
        self._stop(reason)
        if self._stop_file is not None:
            try:
                self._stop_file.write_text(reason + "\n")
            except OSError as failure:  # pragma: no cover - best effort
                print(f"Could not write the stop file: {failure}", file=sys.stderr)
        # The scheduler stops handing out items. Under the tier's numbers it
        # has usually handed out everything already -- the stop file above is
        # what holds the workers -- but a larger collection would not have.
        dsession: Any = self._config.pluginmanager.getplugin("dsession")
        if dsession is not None and not getattr(dsession, "shouldstop", True):
            dsession.shouldstop = reason
        self._write_marker(report, kind)

    def _write_marker(self, report: pytest.TestReport, kind: object) -> None:
        directory = os.environ.get(DIAGNOSTICS_DIR_ENV, "").strip()
        if not directory:
            return
        described = "unclassified" if kind is None else "not retryable"
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / STOPPED_EARLY_MARKER_NAME).write_text(
                f"{_describe(report)} -- {described}\n"
            )
        except OSError as failure:  # pragma: no cover - diagnostics only
            print(f"Could not write the stop marker: {failure}", file=sys.stderr)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self._is_worker or self.stopped_at is None:
            return
        collected = self._session.testscollected if self._session else 0
        not_started = max(collected - len(self._reported), 0)
        terminalreporter.write_sep(
            "!",
            f"registry-live stopped at the first real failure: {self.stopped_at}; "
            f"{not_started} of {collected} scenario(s) not started",
            red=True,
        )

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        if self._owned_dir is not None:
            shutil.rmtree(self._owned_dir, ignore_errors=True)
