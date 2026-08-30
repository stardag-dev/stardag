"""Unit tier: the gating in ``stardag.testing.modal._live``.

No credentials and no network — the workspace lookup is monkeypatched. These
cover the guard's own decisions, which are otherwise only exercised by the
live tier they gate (and therefore not at all when it is skipped).

Note ``pytest.fail`` and ``pytest.skip`` raise ``BaseException`` subclasses,
so the distinction between them is asserted with ``pytest.fail.Exception`` /
``pytest.skip.Exception`` rather than a bare ``Exception``.
"""

import pytest

from stardag.testing.modal import _live

FAILED = pytest.fail.Exception
SKIPPED = pytest.skip.Exception


@pytest.fixture(autouse=True)
def _clear_workspace_cache():
    """The workspace lookup is cached across calls; isolate each test."""
    _live._workspace_cache = _live._WORKSPACE_UNRESOLVED
    yield
    _live._workspace_cache = _live._WORKSPACE_UNRESOLVED


@pytest.fixture(autouse=True)
def _no_profile_guard(monkeypatch):
    """Keep these tests about the workspace check alone."""
    monkeypatch.delenv("STARDAG_MODAL_TEST_PROFILE", raising=False)


@pytest.fixture
def workspace(monkeypatch):
    """Pin what the credentials resolve to, without touching Modal."""

    def _set(name: str | None):
        monkeypatch.setattr(_live, "_active_modal_workspace", lambda: name)

    return _set


@pytest.fixture
def volume_spy(monkeypatch):
    """Record whether the credential check was reached, without a network call."""
    import modal

    calls: list[str] = []

    def _from_name(name, **kwargs):
        calls.append(name)
        raise AssertionError("stop here")  # not reached in mismatch cases

    monkeypatch.setattr(modal.Volume, "from_name", staticmethod(_from_name))
    return calls


class TestWorkspaceAssertion:
    """``STARDAG_MODAL_TEST_WORKSPACE`` checks the token, not a label."""

    def test_matching_workspace_falls_through_to_the_credential_check(
        self, monkeypatch, workspace, volume_spy
    ):
        monkeypatch.setenv("STARDAG_MODAL_TEST_WORKSPACE", "some-workspace")
        monkeypatch.setenv("STARDAG_MODAL_LIVE_TESTS", "1")
        workspace("some-workspace")

        with pytest.raises(AssertionError):
            _live.live_modal_guard("a-volume")
        assert volume_spy == ["a-volume"]

    def test_mismatched_workspace_fails_in_require_mode(self, monkeypatch, workspace):
        monkeypatch.setenv("STARDAG_MODAL_TEST_WORKSPACE", "intended-workspace")
        monkeypatch.setenv("STARDAG_MODAL_LIVE_TESTS", "1")
        workspace("some-other-workspace")

        with pytest.raises(FAILED) as exc:
            _live.live_modal_guard()
        assert "some-other-workspace" in str(exc.value)
        assert "intended-workspace" in str(exc.value)

    def test_mismatched_workspace_skips_in_auto_mode(self, monkeypatch, workspace):
        monkeypatch.setenv("STARDAG_MODAL_TEST_WORKSPACE", "intended-workspace")
        monkeypatch.delenv("STARDAG_MODAL_LIVE_TESTS", raising=False)
        workspace("some-other-workspace")

        with pytest.raises(SKIPPED):
            _live.live_modal_guard()

    def test_unresolvable_workspace_is_a_failure_not_a_pass(
        self, monkeypatch, workspace
    ):
        """A lookup that returns nothing must not satisfy the assertion.

        This is the case that matters: if an unresolvable workspace counted as
        a match, the guard would wave through exactly what it exists to catch.
        """
        monkeypatch.setenv("STARDAG_MODAL_TEST_WORKSPACE", "intended-workspace")
        monkeypatch.setenv("STARDAG_MODAL_LIVE_TESTS", "1")
        workspace(None)

        with pytest.raises(FAILED) as exc:
            _live.live_modal_guard()
        assert "workspace lookup failed" in str(exc.value)

    def test_unset_workspace_var_skips_the_check_entirely(
        self, monkeypatch, volume_spy
    ):
        """Opt-in: no variable, no assertion — local runs are unaffected."""
        monkeypatch.delenv("STARDAG_MODAL_TEST_WORKSPACE", raising=False)
        monkeypatch.setenv("STARDAG_MODAL_LIVE_TESTS", "1")

        def _explode():
            raise AssertionError("workspace must not be looked up when unset")

        monkeypatch.setattr(_live, "_active_modal_workspace", _explode)

        with pytest.raises(AssertionError, match="stop here"):
            _live.live_modal_guard("a-volume")
        assert volume_spy == ["a-volume"]


class TestOrdering:
    """The workspace is asserted before anything writes to it."""

    def test_workspace_is_checked_before_the_volume_is_created(
        self, monkeypatch, workspace, volume_spy
    ):
        """The credential check creates a volume. Verifying the workspace
        afterwards would mean having already written to the wrong one."""
        monkeypatch.setenv("STARDAG_MODAL_TEST_WORKSPACE", "intended-workspace")
        monkeypatch.setenv("STARDAG_MODAL_LIVE_TESTS", "1")
        workspace("some-other-workspace")

        with pytest.raises(FAILED, match="does not match"):
            _live.live_modal_guard()
        assert volume_spy == [], "the wrong workspace was written to"


class TestWorkspaceCaching:
    """Only a successful resolution is cached.

    A None is always read as a mismatch, so caching one would turn a single
    transient failure — or one call from inside a running event loop — into a
    failure for every remaining live module in the process.
    """

    def _patch_lookup(self, monkeypatch, results):
        """Feed `_lookup_modal_workspace_aio` a sequence of outcomes."""
        calls = {"n": 0}

        async def _lookup():
            i = calls["n"]
            calls["n"] += 1
            outcome = results[min(i, len(results) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        import stardag.integration.modal._metadata as meta

        monkeypatch.setattr(meta, "_lookup_modal_workspace_aio", _lookup)
        return calls

    def test_a_failed_lookup_does_not_poison_later_calls(self, monkeypatch):
        calls = self._patch_lookup(
            monkeypatch, [ConnectionError("transient"), "the-workspace"]
        )

        assert _live._active_modal_workspace() is None
        assert _live._active_modal_workspace() == "the-workspace"
        assert calls["n"] == 2, "the failure was cached instead of retried"

    def test_a_successful_lookup_is_cached(self, monkeypatch):
        calls = self._patch_lookup(monkeypatch, ["the-workspace"])

        assert _live._active_modal_workspace() == "the-workspace"
        assert _live._active_modal_workspace() == "the-workspace"
        assert calls["n"] == 1, "the happy path should cost one round trip"

    def test_a_running_event_loop_yields_none_without_caching(self, monkeypatch):
        """`asyncio.run` raises inside a running loop. That is not a
        resolution attempt, so it must not be recorded as one."""
        import asyncio

        calls = self._patch_lookup(monkeypatch, ["the-workspace"])

        async def _in_a_loop():
            return _live._active_modal_workspace()

        assert asyncio.run(_in_a_loop()) is None
        assert calls["n"] == 0, "no lookup should be attempted inside a loop"

        # Outside the loop it resolves normally — the earlier None stuck
        # nothing to the cache.
        assert _live._active_modal_workspace() == "the-workspace"


class TestDisabled:
    """``STARDAG_MODAL_LIVE_TESTS=0`` short-circuits before any network call."""

    def test_disabled_skips_without_looking_anything_up(self, monkeypatch):
        monkeypatch.setenv("STARDAG_MODAL_LIVE_TESTS", "0")
        monkeypatch.setenv("STARDAG_MODAL_TEST_WORKSPACE", "intended-workspace")

        def _explode():
            raise AssertionError("nothing should be looked up when disabled")

        monkeypatch.setattr(_live, "_active_modal_workspace", _explode)

        with pytest.raises(SKIPPED, match="disabled"):
            _live.live_modal_guard()
