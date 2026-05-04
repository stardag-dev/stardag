"""Tests for the volume-reload coalescing logic in ModalMountedVolumeFileTarget.

These tests do **not** require Modal credentials. They install a fake volume
into ``_get_volume`` via monkeypatch so we can directly observe how often
``reload`` / ``reload.aio`` is invoked and ensure that every caller observes
post-reload state on return.

Three behaviours are exercised:

1. **No fixed cooldown.** Sequential calls each reload, so a write that
   landed between two checks is observable on the second check (regardless
   of how recently the first reload completed).
2. **Singleflight coalescing.** N concurrent callers produce exactly one
   reload.
3. **Coalesced callers see fresh state.** Every coalesced caller returns
   *after* the in-flight reload completes — none bail out on a stale view.
"""

import asyncio
import threading
import time

import pytest

try:
    import modal  # noqa: F401  — gate import errors at module level
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.integration.modal import _target as modal_target


class _FakeVolume:
    """Minimal stand-in for ``modal.Volume`` exposing the surface that
    ``_ensure_fresh_volume`` / ``_ensure_fresh_volume_aio`` use.

    Real Modal reloads take 50–200ms; tests of singleflight coalescing need
    the reload to take long enough that concurrent callers actually overlap
    on the lock (otherwise the first caller finishes before the others have
    even checked the timestamp). Defaults to a small but non-trivial delay.

    A ``reload_side_effect`` callback can be supplied to flip external state
    when the reload runs, letting tests assert that callers observe that
    state after the helper returns.
    """

    def __init__(
        self,
        *,
        sync_reload_delay: float = 0.02,
        aio_reload_delay: float = 0.02,
        sync_reload_side_effect=None,
        aio_reload_side_effect=None,
    ) -> None:
        self.reload_count = 0
        self._reload_lock = threading.Lock()
        self._aio_reload_count = 0
        self._sync_reload_delay = sync_reload_delay
        self._aio_reload_delay = aio_reload_delay
        self._sync_reload_side_effect = sync_reload_side_effect
        self._aio_reload_side_effect = aio_reload_side_effect

        # Mimic modal's `volume.reload` (callable) with a `.aio` attribute that
        # is itself awaitable. `_ensure_fresh_volume_aio` calls
        # `_get_volume(name).reload.aio()`, so `reload` must be an object
        # with both `__call__` and an `aio` coroutine.
        outer = self

        class _ReloadCallable:
            def __call__(self) -> None:
                if outer._sync_reload_delay:
                    time.sleep(outer._sync_reload_delay)
                with outer._reload_lock:
                    outer.reload_count += 1
                    if outer._sync_reload_side_effect is not None:
                        outer._sync_reload_side_effect()

            async def aio(self) -> None:
                if outer._aio_reload_delay:
                    await asyncio.sleep(outer._aio_reload_delay)
                outer._aio_reload_count += 1
                if outer._aio_reload_side_effect is not None:
                    outer._aio_reload_side_effect()

        self.reload = _ReloadCallable()

    @property
    def aio_reload_count(self) -> int:
        return self._aio_reload_count


@pytest.fixture(autouse=True)
def reset_volume_reload_state(monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level reload bookkeeping before every test."""
    monkeypatch.setattr(modal_target, "_volume_last_reload_issued", {})
    monkeypatch.setattr(modal_target, "_volume_reload_locks", {})
    monkeypatch.setattr(modal_target, "_volume_reload_aio_locks", {})


# ---------------------------------------------------------------------------
# Sync helper.
# ---------------------------------------------------------------------------


def test_ensure_fresh_volume_reloads_each_sequential_call(
    monkeypatch: pytest.MonkeyPatch,
):
    """No fixed cooldown: sequential calls each trigger a reload."""
    fake = _FakeVolume()
    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)

    modal_target._ensure_fresh_volume("v")
    assert fake.reload_count == 1

    # Immediately again: with the previous cooldown logic this would have
    # been skipped. With singleflight-only, sequential calls always reload.
    modal_target._ensure_fresh_volume("v")
    assert fake.reload_count == 2

    modal_target._ensure_fresh_volume("v")
    assert fake.reload_count == 3


def test_ensure_fresh_volume_concurrent_callers_coalesce_and_see_fresh_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """N concurrent threads → significantly fewer than N reloads, and every
    thread observes a side-effect set during a reload (no caller bails out
    on a pre-reload view).

    Note: with issue-time bookkeeping (a reload only covers writes ≤ its
    issue time), perfect coalescing requires every waiter's ``started`` to
    be ≤ the lock-holder's issue time. Threads that captured ``started``
    after the lock-holder's reload was issued correctly trigger a fresh
    reload of their own — so we only assert "much less than N" reloads,
    not exactly 1. The strict invariant is the side-effect observation."""
    flag = threading.Event()
    fake = _FakeVolume(sync_reload_side_effect=flag.set)
    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)

    n_threads = 16
    barrier = threading.Barrier(n_threads)
    saw_flag = [False] * n_threads

    def worker(i: int):
        barrier.wait()
        modal_target._ensure_fresh_volume("v")
        saw_flag[i] = flag.is_set()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fake.reload_count < n_threads, (
        f"singleflight should coalesce most callers; saw {fake.reload_count} "
        f"reloads for {n_threads} threads"
    )
    assert all(saw_flag), "every caller should observe post-reload state on return"


# ---------------------------------------------------------------------------
# Async helper.
# ---------------------------------------------------------------------------


def test_ensure_fresh_volume_aio_reloads_each_sequential_call(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _FakeVolume()
    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)

    async def _run():
        await modal_target._ensure_fresh_volume_aio("v")
        await modal_target._ensure_fresh_volume_aio("v")
        await modal_target._ensure_fresh_volume_aio("v")

    asyncio.run(_run())
    assert fake.aio_reload_count == 3


def test_ensure_fresh_volume_aio_concurrent_callers_coalesce_and_see_fresh_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """Async equivalent of the sync coalescing test: significantly fewer
    reloads than callers, and every caller observes the side-effect."""
    state = {"flag": False}

    def flip():
        state["flag"] = True

    fake = _FakeVolume(aio_reload_delay=0.05, aio_reload_side_effect=flip)
    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)

    n = 20

    async def _check():
        await modal_target._ensure_fresh_volume_aio("v")
        return state["flag"]

    async def _run():
        return await asyncio.gather(*[_check() for _ in range(n)])

    results = asyncio.run(_run())

    assert fake.aio_reload_count < n, (
        f"singleflight should coalesce most callers; saw {fake.aio_reload_count} "
        f"reloads for {n} coroutines"
    )
    assert all(results), "every caller should observe post-reload state on return"


# ---------------------------------------------------------------------------
# Contract: bookkeeping records issue time, not completion time.
# ---------------------------------------------------------------------------


def test_volume_last_reload_records_issue_time_not_completion_time(
    monkeypatch: pytest.MonkeyPatch,
):
    """A Modal volume reload only flushes writes committed before it was
    *issued*, so a caller starting after another's reload was issued (but
    before it completed) must not falsely short-circuit on it. Verified by
    observing that the bookkeeping timestamp is recorded close to the
    reload's start, not its end."""
    reload_delay = 0.1
    fake = _FakeVolume(sync_reload_delay=reload_delay)
    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)

    before = time.monotonic()
    modal_target._ensure_fresh_volume("v")
    after = time.monotonic()

    issued_at = modal_target._volume_last_reload_issued.get("v")
    assert issued_at is not None
    assert before <= issued_at, "issue time must be after the call started"
    # If the timestamp recorded *completion* it would land near ``after``.
    # Allow generous slack but require it to be much closer to ``before``
    # than to ``after``.
    assert issued_at < after - reload_delay / 2, (
        f"timestamp {issued_at} looks like completion time, not issue time "
        f"(call window: {before}..{after}, reload delay: {reload_delay}s)"
    )


# ---------------------------------------------------------------------------
# Cross-loop safety: cached asyncio.Lock instances are loop-affine.
# ---------------------------------------------------------------------------


def test_ensure_fresh_volume_aio_works_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
):
    """``asyncio.Lock`` instances are bound to the running event loop at
    acquire-time. Stardag uses ``asyncio.run()`` in places, which creates a
    fresh loop each call — a Lock cached at module scope from a previous
    loop must not be reused, otherwise acquiring it can deadlock or raise
    ``RuntimeError``."""
    fake = _FakeVolume()
    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)

    async def _check():
        await modal_target._ensure_fresh_volume_aio("v")

    asyncio.run(_check())
    # Force the second call to take the lock (clear the freshness timestamp).
    modal_target._volume_last_reload_issued.clear()
    # If the lock from the first loop were reused, this would raise / hang.
    asyncio.run(_check())

    assert fake.aio_reload_count == 2
