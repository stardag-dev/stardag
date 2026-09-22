"""The discriminator that decides whether a red tier is retried.

Pure logic, so it lives here rather than under ``tests_registry_live/``
with the code it covers. This directory runs in ordinary CI on every pull
request; the live tier runs only on one that touches its paths, and only
against a Modal stack. The discriminator wants the wider net, because it
is the one part of the tier's retry that can go wrong *silently*: one
that started saying yes to assertion failures would turn a check which
has reported three real product defects into one that runs twice and
shrugs, and every scenario around it would still pass.

It needs none of this directory's docker-compose services.
"""

from __future__ import annotations

import httpx
import pytest
from stardag.exceptions import APIError

from stardag_integration_tests.registry_live import _diagnostics
from stardag_integration_tests.registry_live._diagnostics import (
    BOOT_PROBE_PROMPT_SECONDS,
    CLASSIFICATION_FAILED,
    NON_TIMEOUT_MARKER_NAME,
    TIMEOUT_MARKER_NAME,
    BootProbe,
    record_non_timeout_failure,
    record_transport_timeout,
    transport_timeout,
)
from stardag_integration_tests.registry_live._harness import (
    BootCheckUnanswered,
    Deployment,
    RegistryContainerRecycled,
)

_REQUEST = httpx.Request("GET", "https://registry.invalid/api/v1/builds")


def _raised(error: BaseException) -> BaseException:
    """Raise and catch ``error``, so it carries what a real raise gives it.

    A bare instance has no ``__traceback__`` and no ``__context__``; the
    discriminator walks both, so every test here hands it an exception
    that has actually been through a ``raise``.
    """
    try:
        raise error
    except BaseException as caught:  # noqa: BLE001 - that is the point
        return caught


def test_a_bare_read_timeout_is_a_transport_timeout() -> None:
    error = _raised(httpx.ReadTimeout("timed out", request=_REQUEST))
    assert transport_timeout(error) is error


def test_a_timeout_reraised_from_a_wrapper_is_still_found() -> None:
    """``raise ... from`` is how the SDK's own helpers surface one."""
    timeout = httpx.ConnectTimeout("timed out", request=_REQUEST)
    try:
        try:
            raise timeout
        except httpx.ConnectTimeout as cause:
            raise RuntimeError("the lease call failed") from cause
    except RuntimeError as wrapper:
        assert transport_timeout(wrapper) is timeout


@pytest.mark.parametrize(
    "answered",
    [
        pytest.param(
            APIError("register failed", status_code=500),
            id="the-sdk-turns-a-status-into-an-APIError",
        ),
        pytest.param(
            httpx.HTTPStatusError(
                "401", request=_REQUEST, response=httpx.Response(401, request=_REQUEST)
            ),
            id="raise_for_status-on-the-harness-own-calls",
        ),
    ],
)
def test_an_http_error_status_is_never_retried(answered: Exception) -> None:
    """The registry answered. Whatever it said is a result, not a lost one.

    Both of this tier's real product finds arrived in exactly these two
    shapes -- a 500 from ``tasks/bulk``, a 401 from a replaced container.
    """
    assert transport_timeout(_raised(answered)) is None


def test_a_status_error_anywhere_in_the_chain_wins_over_a_timeout() -> None:
    """Conservative on purpose: any answer at all disqualifies the retry."""
    try:
        try:
            raise APIError("register failed", status_code=500)
        except APIError:
            raise httpx.ReadTimeout("and then it stopped answering", request=_REQUEST)
    except httpx.ReadTimeout as error:
        assert transport_timeout(error) is None


def test_an_assertion_is_never_retried_even_after_a_timeout() -> None:
    """The scenario reached a judgement; retrying it is the thing to avoid."""
    try:
        try:
            raise httpx.ReadTimeout("timed out", request=_REQUEST)
        except httpx.ReadTimeout:
            raise AssertionError("the build never reached a terminal status")
    except AssertionError as error:
        assert transport_timeout(error) is None


def test_the_recycle_assertion_is_exempt_by_type_not_by_phase() -> None:
    """The one failure CI must still be allowed to retry over.

    Named by its own type because the phase cannot identify it: fixtures
    tear down through the registry too, so an ordinary teardown failure
    has to forbid the retry while this one must not.
    """
    error = _raised(RegistryContainerRecycled("boot id changed"))
    assert isinstance(error, AssertionError)
    # Still not a transport timeout, so the retry it enables is the
    # recycle one and not this issue's.
    assert transport_timeout(error) is None


def test_the_boot_check_failure_is_still_a_transport_timeout() -> None:
    """It wraps the timeout rather than replacing it, so the chain still finds it.

    ``BootCheckUnanswered`` exists to say "my probe is already done", and
    it must buy that without costing the classification: it is a
    ``RuntimeError``, not an ``AssertionError``, so the discriminator
    walks through to the timeout underneath.
    """
    timeout = httpx.ReadTimeout("timed out", request=_REQUEST)
    try:
        try:
            raise timeout
        except httpx.ReadTimeout as cause:
            raise BootCheckUnanswered("no answer in six attempts") from cause
    except BootCheckUnanswered as error:
        assert transport_timeout(error) is timeout


def test_only_the_boot_check_skips_the_probe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixture timing out in teardown is probed like anything else.

    The phase cannot decide this: ``slot_limit`` deletes a concurrency
    limit in its teardown, so a teardown timeout is not necessarily the
    boot check. Inferring it from the phase swallowed the probe for
    exactly that case.
    """
    monkeypatch.setenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", str(tmp_path))
    probes: list[str] = []
    monkeypatch.setattr(
        _diagnostics,
        "probe_boot",
        lambda url, **k: (
            probes.append(url)
            or BootProbe(answered=True, elapsed=0.3, boot_id="boot-one", error=None)
        ),
    )
    timeout = httpx.ReadTimeout("timed out", request=_REQUEST)

    probe = record_transport_timeout(
        _deployment(),
        nodeid="tests_registry_live/test_x.py::test_a",
        phase="teardown",
        error=timeout,
        timeout=timeout,
        already_probed=False,
    )
    assert len(probes) == 1
    assert probe.probed is True
    assert probe.label("boot-one") == "HYPOTHESIS B"

    skipped = record_transport_timeout(
        _deployment(),
        nodeid="tests_registry_live/test_y.py::test_b",
        phase="teardown",
        error=timeout,
        timeout=timeout,
        already_probed=True,
    )
    assert len(probes) == 1
    assert skipped.probed is False


def test_a_timeout_inside_an_exception_group_is_found() -> None:
    """pytest wraps multiple failing fixture finalizers in one.

    Not hypothetical: this tier has two teardown fixtures that talk to
    the registry, and two raising finalizers were confirmed to arrive as
    an ``ExceptionGroup`` against this pytest. Missing it would forbid a
    retry the run was entitled to and lose the probe with it.
    """
    timeout = httpx.PoolTimeout("no connection free", request=_REQUEST)
    group = _raised(ExceptionGroup("teardown", [timeout, timeout]))  # noqa: F821
    assert transport_timeout(group) is timeout


def test_a_group_holding_a_real_failure_is_not_retryable() -> None:
    """One veto in the group is enough, exactly as in a cause chain."""
    group = _raised(
        ExceptionGroup(  # noqa: F821
            "teardown",
            [
                httpx.ReadTimeout("timed out", request=_REQUEST),
                APIError("cleanup failed", status_code=500),
            ],
        )
    )
    assert transport_timeout(group) is None


def test_a_failure_with_no_exception_is_still_recorded(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strict ``xfail`` that passes is a failed report carrying nothing.

    Dropping it would leave the run retryable over an XPASS -- confirmed
    to be the shape pytest produces, rather than assumed.
    """
    monkeypatch.setenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", str(tmp_path))
    record_non_timeout_failure(nodeid="x::y", phase="call", error=None)
    assert (tmp_path / NON_TIMEOUT_MARKER_NAME).read_text() == (
        "x::y [call] -- failed with no exception\n"
    )


def test_an_unwritable_marker_fails_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI reads an absent marker as "nothing real broke", so say so aloud.

    The sentinel goes to stderr rather than to a file on purpose: the
    thing that just failed is writing files.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", str(blocked / "sub"))
    record_non_timeout_failure(
        nodeid="x::y", phase="call", error=AssertionError("boom")
    )
    assert CLASSIFICATION_FAILED in capsys.readouterr().err


def test_a_connection_error_is_not_a_timeout() -> None:
    """Narrow by design: the evidence names timeouts and nothing else."""
    error = _raised(httpx.ConnectError("connection refused", request=_REQUEST))
    assert transport_timeout(error) is None


def test_a_circular_context_chain_terminates() -> None:
    """An exception raised inside its own handler can close the loop."""
    first = httpx.ReadTimeout("timed out", request=_REQUEST)
    second = ValueError("while handling the above")
    first.__context__ = second
    second.__context__ = first
    assert transport_timeout(second) is first


_SLOW = BOOT_PROBE_PROMPT_SECONDS + 1.0


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        pytest.param(
            BootProbe(answered=True, elapsed=0.2, boot_id="abc", error=None),
            "HYPOTHESIS B",
            id="prompt-answer-means-the-database-path-is-blocked",
        ),
        pytest.param(
            BootProbe(answered=False, elapsed=15.0, boot_id=None, error="x"),
            "HYPOTHESIS A",
            id="no-answer-means-nothing-is-serving",
        ),
        # The finding the threshold exists for: an endpoint returning a
        # string held in a closure, after several seconds, is evidence of a
        # starved container -- not of a healthy one behind a blocked
        # database. "It answered" is not the question.
        pytest.param(
            BootProbe(answered=True, elapsed=_SLOW, boot_id="abc", error=None),
            "HYPOTHESIS A",
            id="slow-answer-is-starvation-not-a-blocked-database",
        ),
        pytest.param(
            BootProbe(answered=True, elapsed=0.2, boot_id="def", error=None),
            "RECYCLE",
            id="a-different-boot-id-outranks-both",
        ),
    ],
)
def test_the_probe_names_the_hypothesis_it_supports(
    probe: BootProbe, expected: str
) -> None:
    assert probe.label("abc") == expected


def test_the_verdict_prose_cannot_disagree_with_the_counted_label() -> None:
    """The artifact's sentence and CI's word are one decision, not two."""
    for probe in (
        BootProbe(answered=True, elapsed=0.2, boot_id="abc", error=None),
        BootProbe(answered=True, elapsed=_SLOW, boot_id="abc", error=None),
        BootProbe(answered=False, elapsed=15.0, boot_id=None, error="x"),
        BootProbe(answered=True, elapsed=0.2, boot_id="def", error=None),
        BootProbe(answered=False, elapsed=0.0, boot_id=None, error=None, probed=False),
    ):
        assert probe.verdict("abc").startswith(probe.label("abc") + " -- ")


def test_a_teardown_timeout_does_not_claim_a_probe_it_never_ran() -> None:
    """Its conclusion is hypothesis A, but not on the strength of a 0.0s probe."""
    probe = BootProbe(
        answered=False, elapsed=0.0, boot_id=None, error=None, probed=False
    )
    verdict = probe.verdict("abc")
    assert probe.label("abc") == "HYPOTHESIS A"
    assert "no probe was run" in verdict
    assert "0.0s" not in verdict


def test_a_non_timeout_failure_is_named_where_ci_will_refuse_the_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twelve workers share a run; one of these must end it for all of them."""
    monkeypatch.setenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", str(tmp_path))
    record_non_timeout_failure(
        nodeid="tests_registry_live/test_x.py::test_a",
        phase="call",
        error=AssertionError("the build never completed"),
    )
    # A fixture failing for real must end the run exactly as a scenario
    # body would: fixtures here talk to the registry.
    record_non_timeout_failure(
        nodeid="tests_registry_live/test_y.py::test_b",
        phase="setup",
        error=RuntimeError("boom"),
    )

    # Appended rather than rewritten: each xdist worker is its own process,
    # and a whole-file write would leave only whichever failed last.
    lines = (tmp_path / NON_TIMEOUT_MARKER_NAME).read_text().splitlines()
    assert lines == [
        "tests_registry_live/test_x.py::test_a [call] -- AssertionError",
        "tests_registry_live/test_y.py::test_b [setup] -- RuntimeError",
    ]


def test_nothing_is_written_when_no_diagnostics_directory_is_configured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer running the tier locally configures none of this."""
    monkeypatch.delenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", raising=False)
    record_non_timeout_failure(
        nodeid="x::y", phase="call", error=AssertionError("boom")
    )
    assert list(tmp_path.iterdir()) == []


def _deployment(api_url: str = "https://registry.invalid") -> Deployment:
    return Deployment(
        api_url=api_url,
        modal_environment="dev-test",
        workspace_slug="w",
        environment_slug="e",
        workspace_id="wid",
        environment_id="eid",
        api_key="k",
        boot_id="boot-one",
    )


def test_a_recycle_the_probe_finds_is_recorded_as_a_recycle(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different boot id is a recycle, whichever read noticed it.

    The two failures want different recoveries — a recycle's retry
    re-provisions, because the replacement's database is empty — so the
    probe must say so where CI acts on it rather than leave it to the
    post-scenario check, whose own read may get no answer either.
    """
    monkeypatch.setenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setenv(
        "STARDAG_REGISTRY_LIVE_RECYCLE_MARKER", str(tmp_path / "registry-recycled")
    )
    monkeypatch.setattr(
        _diagnostics,
        "probe_boot",
        lambda *a, **k: BootProbe(
            answered=True, elapsed=0.3, boot_id="boot-two", error=None
        ),
    )

    record_transport_timeout(
        _deployment(),
        nodeid="tests_registry_live/test_x.py::test_a",
        phase="call",
        error=httpx.ReadTimeout("timed out", request=_REQUEST),
        timeout=httpx.ReadTimeout("timed out", request=_REQUEST),
    )

    assert (tmp_path / "registry-recycled").read_text() == "boot-one -> boot-two\n"
    # And deliberately *not* in the timeout marker: that file is the count
    # this issue escalates on, and a recycle counted as a transport timeout
    # would make the count say the opposite of what happened.
    assert not (tmp_path / TIMEOUT_MARKER_NAME).exists()
    assert len(list(tmp_path.glob("timeout-*.txt"))) == 1


def test_two_phases_of_one_test_leave_two_records(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture teardown runs after a failed call, so one test can time out twice.

    Only the call-phase record carries a probe, so a filename that did not
    distinguish the phases would lose exactly the evidence worth keeping.
    """
    monkeypatch.setenv("STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setattr(
        _diagnostics,
        "probe_boot",
        lambda *a, **k: BootProbe(
            answered=True, elapsed=0.3, boot_id="boot-one", error=None
        ),
    )
    timeout = httpx.ReadTimeout("timed out", request=_REQUEST)

    for phase in ("call", "teardown"):
        record_transport_timeout(
            _deployment(),
            nodeid="tests_registry_live/test_x.py::test_a",
            phase=phase,
            error=timeout,
            timeout=timeout,
        )

    records = sorted(path.name for path in tmp_path.glob("timeout-*.txt"))
    assert len(records) == 2
    assert records[0].startswith("timeout-call-")
    assert records[1].startswith("timeout-teardown-")
    assert len((tmp_path / TIMEOUT_MARKER_NAME).read_text().splitlines()) == 2
