"""A process applying a build's settings serves one build at a time.

Settings are process-global environment variables (D4), so
``settings_applied`` refuses a second build entering while another build's
settings are installed, rather than letting either run under the other's
values or lose its own to the other's restore.
"""

from __future__ import annotations

import asyncio
import os
import threading
from uuid import uuid4

import pytest

from stardag.build._settings import SettingsError, settings_applied, settings_owner

KEY = "SD_TEST_SETTINGS_OWNER_KEY"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)


def test_applies_and_restores():
    build = uuid4()
    with settings_applied({KEY: "a"}, owner=build):
        assert os.environ[KEY] == "a"
    assert KEY not in os.environ


def test_another_builds_settings_are_refused_while_installed():
    a, b = uuid4(), uuid4()
    with settings_applied({KEY: "a"}, owner=a):
        with pytest.raises(SettingsError, match="already installed"):
            with settings_applied({KEY: "b"}, owner=b):
                pass  # pragma: no cover
        # The refused entry changed nothing.
        assert os.environ[KEY] == "a"
    assert KEY not in os.environ


def test_equal_or_empty_settings_do_not_make_an_overlap_safe():
    """Identical values would still be removed by the first exit's restore,
    and empty settings would run under the other build's values."""
    a, b = uuid4(), uuid4()
    with settings_applied({}, owner=a):
        with pytest.raises(SettingsError):
            with settings_applied({}, owner=b):
                pass  # pragma: no cover


def test_the_same_build_nests():
    build = uuid4()
    with settings_applied({KEY: "a"}, owner=build):
        with settings_applied({KEY: "a"}, owner=build):
            assert os.environ[KEY] == "a"
        assert os.environ[KEY] == "a"
    assert KEY not in os.environ


def test_the_process_is_free_again_after_exit_and_after_an_error():
    a, b = uuid4(), uuid4()
    with pytest.raises(RuntimeError):
        with settings_applied({KEY: "a"}, owner=a):
            raise RuntimeError("boom")
    with settings_applied({KEY: "b"}, owner=b):
        assert os.environ[KEY] == "b"


def test_interleaved_coroutines_are_refused():
    """The deployed shape: a tick awaits inside ``settings_applied``. Two
    ticks packed onto one event loop must fail loudly, not interleave."""
    barrier_entered = asyncio.Event()
    release = asyncio.Event()

    async def first():
        with settings_applied({KEY: "a"}, owner="build-a"):
            barrier_entered.set()
            await release.wait()
            return os.environ[KEY]

    async def second():
        await barrier_entered.wait()
        try:
            with settings_applied({KEY: "b"}, owner="build-b"):
                return "entered"  # pragma: no cover
        except SettingsError:
            return "refused"
        finally:
            release.set()

    async def main():
        return await asyncio.gather(first(), second())

    assert asyncio.run(main()) == ["a", "refused"]


def test_threads_are_refused_too():
    """A sync worker packed by Modal runs on threads."""
    entered, release = threading.Event(), threading.Event()
    outcome: list[str] = []

    def hold():
        with settings_owner("build-a"):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(timeout=5)
    try:
        with settings_owner("build-b"):
            outcome.append("entered")  # pragma: no cover
    except SettingsError:
        outcome.append("refused")
    finally:
        release.set()
        holder.join()
    assert outcome == ["refused"]
