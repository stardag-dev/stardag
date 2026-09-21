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

from stardag_integration_tests.registry_live._diagnostics import (
    BootProbe,
    transport_timeout,
)

_REQUEST = httpx.Request("GET", "https://registry.invalid/api/v1/builds")


def _raised(error: BaseException) -> BaseException:
    """``error`` with the ``__traceback__`` and links a real raise gives it."""
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


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (
            BootProbe(answered=True, elapsed=0.2, boot_id="abc", error=None),
            "HYPOTHESIS B",
        ),
        (
            BootProbe(answered=False, elapsed=15.0, boot_id=None, error="x"),
            "HYPOTHESIS A",
        ),
        (BootProbe(answered=True, elapsed=0.2, boot_id="def", error=None), "RECYCLE"),
    ],
)
def test_the_probe_names_the_hypothesis_it_supports(
    probe: BootProbe, expected: str
) -> None:
    assert probe.label("abc") == expected
