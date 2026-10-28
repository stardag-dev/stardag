"""The registry-live tier stops at its first real failure, and only then.

``_stop_early`` is the one part of STA-172 that can be proven without
Modal, and the part worth proving: whether pytest-xdist honours the stop
is a property of xdist's scheduling, not of the plugin's intent. So these
run a real ``pytest -n 2`` in a subprocess, over dummy scenarios that
record whether they started and whether they finished.

The shape every case uses: four items, two per worker, which is how
``LoadScheduling`` hands them out (each worker holds both of its items
from the start). The first item fails, or not, after a moment; the third
is running on the other worker at that point and takes longer. So:

- ``first`` decides the case;
- ``second`` is queued behind it on the same worker -- it must not start
  after a real failure;
- ``inflight`` is already running elsewhere -- it must finish either way;
- ``fourth`` is queued behind ``inflight`` -- it must not start after a
  real failure, which only the stop file can prevent. Checked by hand when
  this was written: with the workers' stop-file check disabled, the
  controller's ``shouldstop`` alone still lets ``fourth`` run, because the
  shutdown it sends is queued behind the items the worker already holds.

Like the discriminator's tests, these need none of this directory's
docker-compose services.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live._diagnostics import DIAGNOSTICS_DIR_ENV
from stardag_integration_tests.registry_live._stop_early import (
    STOP_EARLY_ENV,
    STOPPED_EARLY_MARKER_NAME,
)

_CONFTEST = """
from stardag_integration_tests.registry_live import _stop_early


def pytest_configure(config):
    _stop_early.register(config)
"""

# ``{first}`` is the body of the deciding item; ``{fixture}`` lets a case
# fail in setup or teardown instead.
_SCENARIOS = """
import os
import time
from pathlib import Path

import httpx
import pytest

from stardag_integration_tests.registry_live._harness import (
    RegistryContainerRecycled,
)

RAN = Path(os.environ["RAN_DIR"])
_REQUEST = httpx.Request("GET", "https://registry.invalid/api/v1/builds")


def mark(name, what):
    (RAN / f"{{name}}.{{what}}").touch()


@pytest.fixture
def phased():
{fixture}


{decorator}
def test_1_first(phased):
    mark("first", "started")
    time.sleep(0.5)
{first}
    mark("first", "finished")


def test_2_second():
    mark("second", "started")
    mark("second", "finished")


def test_3_inflight():
    mark("inflight", "started")
    time.sleep(4)
    mark("inflight", "finished")


def test_4_fourth():
    mark("fourth", "started")
    mark("fourth", "finished")
"""

_NO_FIXTURE = "    yield"


def _run(
    tmp_path: Path,
    *,
    first: str = "    pass",
    fixture: str = _NO_FIXTURE,
    decorator: str = "",
    enabled: bool = True,
    workers: str = "2",
) -> tuple[set[str], subprocess.CompletedProcess[str], Path]:
    project = tmp_path / "project"
    project.mkdir()
    ran = tmp_path / "ran"
    ran.mkdir()
    diagnostics = tmp_path / "diagnostics"
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "conftest.py").write_text(_CONFTEST)
    (project / "test_scenarios.py").write_text(
        _SCENARIOS.format(first=first, fixture=fixture, decorator=decorator)
    )

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "GIT_"))
        and key not in (STOP_EARLY_ENV, DIAGNOSTICS_DIR_ENV)
    }
    env["RAN_DIR"] = str(ran)
    env[DIAGNOSTICS_DIR_ENV] = str(diagnostics)
    if enabled:
        env[STOP_EARLY_ENV] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            workers,
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-v",
            "test_scenarios.py",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {path.name for path in ran.iterdir()}, result, diagnostics


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _all_ran(marks: set[str], *, first_finished: bool) -> bool:
    expected = {
        "first.started",
        "second.started",
        "second.finished",
        "inflight.started",
        "inflight.finished",
        "fourth.started",
        "fourth.finished",
    }
    if first_finished:
        expected.add("first.finished")
    return marks == expected


def _stopped(marks: set[str]) -> bool:
    """In-flight work finished; nothing queued behind the failure started."""
    return marks == {
        "first.started",
        "inflight.started",
        "inflight.finished",
    } or marks == {
        # A failure in ``first``'s teardown: its body had finished.
        "first.started",
        "first.finished",
        "inflight.started",
        "inflight.finished",
    }


def test_a_real_failure_stops_scheduling_and_lets_in_flight_finish(
    tmp_path: Path,
) -> None:
    marks, result, diagnostics = _run(tmp_path, first="    assert False, 'real'")
    assert _stopped(marks), (marks, _output(result))
    assert result.returncode != 0
    marker = (diagnostics / STOPPED_EARLY_MARKER_NAME).read_text()
    assert "test_scenarios.py::test_1_first [call]" in marker
    assert "stopped at the first real failure" in _output(result)


def test_an_http_error_status_is_real(tmp_path: Path) -> None:
    first = textwrap.indent(
        "raise httpx.HTTPStatusError('500', request=_REQUEST, "
        "response=httpx.Response(500, request=_REQUEST))",
        "    ",
    )
    marks, result, _ = _run(tmp_path, first=first)
    assert _stopped(marks), (marks, _output(result))


def test_a_transport_fault_does_not_stop_the_run(tmp_path: Path) -> None:
    first = "    raise httpx.ReadTimeout('timed out', request=_REQUEST)"
    marks, result, diagnostics = _run(tmp_path, first=first)
    assert _all_ran(marks, first_finished=False), (marks, _output(result))
    assert result.returncode == 1
    assert not (diagnostics / STOPPED_EARLY_MARKER_NAME).exists()


def test_a_recycle_does_not_stop_the_run(tmp_path: Path) -> None:
    first = "    raise RegistryContainerRecycled('boot id changed')"
    marks, result, _ = _run(tmp_path, first=first)
    assert _all_ran(marks, first_finished=False), (marks, _output(result))


def test_a_real_failure_in_teardown_stops_the_run(tmp_path: Path) -> None:
    fixture = "    yield\n    assert False, 'cleanup failed for real'"
    marks, result, diagnostics = _run(tmp_path, fixture=fixture)
    assert _stopped(marks), (marks, _output(result))
    marker = (diagnostics / STOPPED_EARLY_MARKER_NAME).read_text()
    assert "[teardown]" in marker


def test_a_transport_fault_in_setup_does_not_stop_the_run(tmp_path: Path) -> None:
    fixture = (
        "    raise httpx.ConnectError('refused', request=_REQUEST)\n"
        "    yield  # pragma: no cover"
    )
    marks, result, _ = _run(tmp_path, fixture=fixture)
    # ``first`` never started: its setup failed. Everything else ran.
    assert marks == {
        "second.started",
        "second.finished",
        "inflight.started",
        "inflight.finished",
        "fourth.started",
        "fourth.finished",
    }, (marks, _output(result))


def test_a_strict_xpass_is_real(tmp_path: Path) -> None:
    # A failed report carrying no exception at all: ``first`` passes under a
    # strict xfail, which makes it a failure with nothing to classify.
    marks, result, diagnostics = _run(
        tmp_path, decorator="@pytest.mark.xfail(strict=True)"
    )
    assert _stopped(marks), (marks, _output(result))
    assert "[call]" in (diagnostics / STOPPED_EARLY_MARKER_NAME).read_text()


def test_disabled_it_changes_nothing(tmp_path: Path) -> None:
    marks, result, diagnostics = _run(
        tmp_path, first="    assert False, 'real'", enabled=False
    )
    assert _all_ran(marks, first_finished=False), (marks, _output(result))
    assert not (diagnostics / STOPPED_EARLY_MARKER_NAME).exists()


@pytest.mark.parametrize("workers", ["0"])
def test_without_xdist_it_stops_too(tmp_path: Path, workers: str) -> None:
    marks, result, _ = _run(tmp_path, first="    assert False, 'real'", workers=workers)
    # Serial: nothing was in flight, so nothing after ``first`` ran.
    assert marks == {"first.started"}, (marks, _output(result))
