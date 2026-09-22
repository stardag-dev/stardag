"""The access-log join that turns a timeout's facts into a hypothesis.

Companion to ``test_registry_live_diagnostics``, and here for the same
reason: pure logic, run on every pull request rather than only on one
that touches the live tier.

What it is guarding is narrower than "the verdict is right", which no
test can check. It is that **the verdict is supported**. The instrument's
first version concluded "the database path is blocked" from a probe that
could not see the database, and wrote it into an artifact designed to be
believed; the run that exercised it had a maximum handler time of 460ms.
So the cases below are mostly about refusing to conclude: no log, a log
that does not reach the moment in question, an exception that did not say
what it was asking for.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live import diagnose
from stardag_integration_tests.registry_live._diagnostics import CONTAINER_SERVING
from stardag_integration_tests.registry_live.diagnose import (
    REGISTRY_LOG_NAME,
    VERDICTS_NAME,
    parse_access_log,
    report,
    verdict_for,
)

_AT = dt.datetime(2026, 9, 22, 10, 14, 41, tzinfo=dt.timezone.utc)


def _line(
    at: dt.datetime,
    method: str = "POST",
    path: str = "/api/v1/builds",
    *,
    status: int = 201,
    duration: str = "320.9 ms",
    execution: str = "203.9 ms",
) -> str:
    stamp = at.strftime("%Y-%m-%d %H:%M:%S+00:00")
    return (
        f"{stamp} ta-01M349DXEG    {method} {path} -> {status} Created  "
        f"(duration: {duration}, execution: {execution})"
    )


def _log(directory: Path, *lines: str) -> None:
    (directory / REGISTRY_LOG_NAME).write_text(
        "# registry in Modal environment ci-pr-1\n" + "\n".join(lines) + "\n"
    )


def _occurrence(**overrides: object) -> dict:
    occurrence: dict[str, object] = {
        "nodeid": "tests_registry_live/test_x.py::test_a",
        "phase": "call",
        "observed_at": _AT.isoformat(),
        "request_method": "POST",
        "request_path": "/api/v1/builds",
        "probe_label": CONTAINER_SERVING,
    }
    occurrence.update(overrides)
    return occurrence


# --- parsing -------------------------------------------------------------


def test_the_access_log_is_parsed_out_of_everything_else(tmp_path: Path) -> None:
    """Application logging shares the file and must not break the parse."""
    _log(
        tmp_path,
        "2026-09-22 10:08:50+00:00 ta-01M349DXEG  INFO booting, id=abc",
        _line(_AT, "GET", "/health", status=200, duration="5.88 s"),
        "some line with no timestamp at all",
        _line(_AT, duration="1.09 s", execution="205.0 ms"),
    )
    log = parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    assert log.present is True
    assert [row.path for row in log.served] == ["/health", "/api/v1/builds"]
    # Seconds and milliseconds both normalise to seconds, which is what
    # every threshold below is expressed in.
    assert log.served[0].duration == pytest.approx(5.88)
    assert log.served[1].execution == pytest.approx(0.205)


def test_a_missing_log_is_absent_rather_than_empty(tmp_path: Path) -> None:
    """The two mean different things and only one supports a verdict."""
    log = parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    assert log.present is False
    assert log.served == []


# --- the verdicts --------------------------------------------------------


def test_a_slow_handler_is_the_database_path(tmp_path: Path) -> None:
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30), execution="21.4 s"))
    verdict, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "HYPOTHESIS B"
    assert "21.4s inside the handler" in " ".join(evidence)


def test_a_fast_handler_behind_a_long_queue_is_a_starved_container(
    tmp_path: Path,
) -> None:
    _log(
        tmp_path,
        _line(_AT - dt.timedelta(seconds=30), duration="26.0 s", execution="180.0 ms"),
    )
    verdict, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "HYPOTHESIS A"
    assert "25.8s of it was queueing" in " ".join(evidence)


def test_a_server_that_saw_nothing_slow_is_the_third_answer(tmp_path: Path) -> None:
    """The finding the whole pass exists for.

    The server handled the request in milliseconds and the client waited
    thirty seconds. Neither of the two original hypotheses fits, and the
    instrument used to answer B here with no evidence at all.
    """
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30)))
    verdict, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "HYPOTHESIS C"
    assert "never received" in " ".join(evidence)


def test_a_request_the_server_never_logged_is_also_the_third_answer(
    tmp_path: Path,
) -> None:
    """Absence counts only because the log is shown to cover the window.

    So the log has to reach back past the whole lookback, not merely to
    somewhere near the failure -- which is the distinction the next test
    exercises from the other side.
    """
    _log(
        tmp_path,
        _line(_AT - dt.timedelta(seconds=120), "GET", "/health", status=200),
        _line(_AT + dt.timedelta(seconds=5), "GET", "/health", status=200),
    )
    verdict, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "HYPOTHESIS C"
    assert "logged no POST /api/v1/builds" in " ".join(evidence)


def test_several_candidates_weaken_a_positive_verdict_but_not_the_third(
    tmp_path: Path,
) -> None:
    """``POST /api/v1/builds`` carries no id, and twelve workers issue it.

    So a slow line in the window is a candidate, not an identification,
    and the evidence has to say which it is. A window in which *nothing*
    was slow needs no such caveat: it rules the other hypotheses out for
    every member at once.
    """
    base = _AT - dt.timedelta(seconds=30)
    _log(
        tmp_path,
        _line(base, execution="21.4 s"),
        _line(base + dt.timedelta(seconds=1)),
    )
    _, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert "candidate rather than an identification" in " ".join(evidence)

    _log(tmp_path, _line(base), _line(base + dt.timedelta(seconds=1)))
    verdict, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "HYPOTHESIS C"
    assert "candidate rather than an identification" not in " ".join(evidence)


# --- refusing to conclude ------------------------------------------------


def test_no_log_is_no_verdict(tmp_path: Path) -> None:
    verdict, _ = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "NO VERDICT"


def test_a_log_that_does_not_reach_the_moment_is_a_gap_not_evidence(
    tmp_path: Path,
) -> None:
    """The difference between "nothing was logged" and "nothing was kept".

    ``modal app logs`` takes a window and a line budget, so a long run
    can produce a log that starts after the failure it is meant to
    explain. Reading that as "the server never saw the request" would
    turn a truncated dump into a confident hypothesis C.
    """
    _log(
        tmp_path,
        _line(_AT + dt.timedelta(minutes=5)),
        _line(_AT + dt.timedelta(minutes=6)),
    )
    verdict, evidence = verdict_for(
        _occurrence(), parse_access_log(tmp_path / REGISTRY_LOG_NAME)
    )
    assert verdict == "NO VERDICT"
    assert "gap in the evidence rather than evidence" in " ".join(evidence)


def test_a_probe_that_found_nothing_serving_is_not_overturned(tmp_path: Path) -> None:
    """A direct observation outranks a later reading of a log.

    The probe asked the container at the moment of failure and got
    nothing. No amount of access log can make that not have happened --
    and the log would be *quiet* in exactly that case, which a naive join
    would read as hypothesis C.
    """
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30)))
    verdict, _ = verdict_for(
        _occurrence(probe_label="HYPOTHESIS A"),
        parse_access_log(tmp_path / REGISTRY_LOG_NAME),
    )
    assert verdict == "HYPOTHESIS A"


def test_a_recycle_keeps_its_own_answer(tmp_path: Path) -> None:
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30)))
    verdict, _ = verdict_for(
        _occurrence(probe_label="RECYCLE"),
        parse_access_log(tmp_path / REGISTRY_LOG_NAME),
    )
    assert verdict == "RECYCLE"


def test_an_unidentifiable_request_falls_back_to_the_window_and_says_so(
    tmp_path: Path,
) -> None:
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30)))
    verdict, evidence = verdict_for(
        _occurrence(request_method=None, request_path=None),
        parse_access_log(tmp_path / REGISTRY_LOG_NAME),
    )
    assert verdict == "HYPOTHESIS C"
    assert "could not be identified from the exception" in " ".join(evidence)


# --- the report ----------------------------------------------------------


def test_the_report_reads_a_retried_attempts_records_too(tmp_path: Path) -> None:
    """``attempt-1/`` holds the occurrences the retry was granted for.

    A pass that read only the top level would have nothing to say about
    the very run that prompted it -- and on a retry that went green, the
    top level holds no records at all.
    """
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30)))
    attempt = tmp_path / "attempt-1"
    attempt.mkdir()
    (attempt / "timeout-call-test-a-1.json").write_text(json.dumps(_occurrence()))

    text = report(tmp_path)
    assert "VERDICT: HYPOTHESIS C" in text
    assert "attempt-1" in text


def test_the_report_says_when_there_is_nothing_to_reconcile(tmp_path: Path) -> None:
    _log(tmp_path, _line(_AT))
    assert "Nothing to\n  reconcile" in report(tmp_path) or "reconcile" in report(
        tmp_path
    )


def test_the_report_flags_a_log_it_could_not_parse_at_all(tmp_path: Path) -> None:
    """A changed log format must read as a broken parser, not as a quiet server.

    This is the pass's own silent-failure mode: if the access line ever
    stops matching, every occurrence becomes "the server saw nothing
    slow", which is hypothesis C stated with total confidence and no
    evidence whatsoever.
    """
    (tmp_path / REGISTRY_LOG_NAME).write_text("some log in a format nobody expected\n")
    text = report(tmp_path)
    assert "no longer matches it" in text


def test_main_writes_the_verdicts_into_the_artifact(tmp_path: Path) -> None:
    _log(tmp_path, _line(_AT - dt.timedelta(seconds=30)))
    (tmp_path / "timeout-call-test-a-1.json").write_text(json.dumps(_occurrence()))

    assert diagnose.main(["--dir", str(tmp_path)]) == 0
    written = (tmp_path / VERDICTS_NAME).read_text()
    assert "VERDICT: HYPOTHESIS C" in written


def test_main_is_never_what_turns_a_run_red(tmp_path: Path) -> None:
    """A green run writes no diagnostics directory at all."""
    assert diagnose.main(["--dir", str(tmp_path / "absent")]) == 0
