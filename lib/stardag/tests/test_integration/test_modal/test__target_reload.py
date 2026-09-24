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


# ---------------------------------------------------------------------------
# A hit older than the current walk is refreshed (S5).
# ---------------------------------------------------------------------------


def _mounted_target(tmp_path, monkeypatch, fake):
    """A ModalMountedVolumeFileTarget over ``tmp_path`` as the mount."""
    from stardag.target import _freshness

    monkeypatch.setattr(modal_target, "_get_volume", lambda _name: fake)
    monkeypatch.setattr(_freshness, "_fence", 0.0)
    target = modal_target.ModalMountedVolumeFileTarget.__new__(
        modal_target.ModalMountedVolumeFileTarget
    )
    target._volume_name = "v"
    target.volume = fake
    target.local_path = tmp_path / "out.json"
    return target


def test_a_hit_from_before_the_walk_is_refreshed_and_a_deletion_seen(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A warm container's view can still hold a file another process
    deleted. Once a walk has begun, a hit from an older view is not trusted:
    the volume is reloaded once, and the deletion the reload brings in is
    what ``exists`` answers."""
    from stardag.target._freshness import begin_observation

    output = tmp_path / "out.json"
    output.write_text("{}")
    fake = _FakeVolume(sync_reload_side_effect=lambda: output.unlink())
    target = _mounted_target(tmp_path, monkeypatch, fake)

    # No walk yet: a hit is a hit, no reload (the old behaviour).
    assert target.exists() is True
    assert fake.reload_count == 0

    begin_observation()
    assert target.exists() is False
    assert fake.reload_count == 1


def test_one_reload_per_walk_serves_every_later_hit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from stardag.target._freshness import begin_observation

    (tmp_path / "out.json").write_text("{}")
    fake = _FakeVolume()
    target = _mounted_target(tmp_path, monkeypatch, fake)

    begin_observation()
    assert target.exists() and target.exists() and target.exists()
    assert fake.reload_count == 1

    begin_observation()
    assert target.exists()
    assert fake.reload_count == 2


@pytest.mark.asyncio
async def test_a_stale_hit_is_refreshed_on_the_async_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from stardag.target._freshness import begin_observation

    output = tmp_path / "out.json"
    output.write_text("{}")
    fake = _FakeVolume(aio_reload_side_effect=lambda: output.unlink())
    target = _mounted_target(tmp_path, monkeypatch, fake)

    begin_observation()
    assert await target.exists_aio() is False
    assert fake.aio_reload_count == 1


@pytest.mark.asyncio
async def test_a_walk_begins_an_observation(monkeypatch: pytest.MonkeyPatch):
    """``walk_aio`` sets the fence, so every completion it asks for is
    answered from a view at least as fresh as the walk."""
    import stardag as sd
    from stardag.build._registration import walk_aio
    from stardag.target import _freshness

    monkeypatch.setattr(_freshness, "_fence", 0.0)
    before = time.monotonic()

    @sd.task
    def fence_probe(x: int) -> int:
        return x

    await walk_aio(fence_probe(x=1), check_stability=False)
    assert _freshness.observation_fence() >= before


# ---------------------------------------------------------------------------
# A miss always reloads, walk or no walk. An earlier version let a miss
# trust "a reload already covered this walk's fence"; the fence is
# process-global and outlives the walk, so a warm worker checking its
# yielded children read a finished child as missing and suspended again
# (registry-live, PR #389: test_shared_structure_scope and S24).
# ---------------------------------------------------------------------------


def test_a_miss_after_a_walk_sees_a_write_landed_since(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A walk ran (fence set) and reloaded; another container then writes a
    child's output. A later miss -- the worker's post-yield completeness
    check, not part of any walk -- must reload and see it."""
    from stardag.target._freshness import begin_observation

    child = tmp_path / "child.json"
    fake = _FakeVolume(sync_reload_side_effect=lambda: None)
    target = _mounted_target(tmp_path, monkeypatch, fake)
    target.local_path = child

    begin_observation()
    assert target.exists() is False  # reload 1; nothing written yet
    assert fake.reload_count == 1

    # Written by another container; visible here only after a reload.
    fake._sync_reload_side_effect = lambda: child.write_text("{}")
    assert target.exists() is True
    assert fake.reload_count == 2


@pytest.mark.asyncio
async def test_a_miss_after_a_walk_sees_a_write_landed_since_async(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from stardag.target._freshness import begin_observation

    child = tmp_path / "child.json"
    fake = _FakeVolume()
    target = _mounted_target(tmp_path, monkeypatch, fake)
    target.local_path = child

    begin_observation()
    assert await target.exists_aio() is False
    fake._aio_reload_side_effect = lambda: child.write_text("{}")
    assert await target.exists_aio() is True
    assert fake.aio_reload_count == 2


def test_a_miss_before_any_walk_always_reloads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Outside of any walk (fence still 0), a miss must keep reloading on
    every call -- unchanged from before the fence existed. Guards against
    treating the "no walk, never reloaded" default (both 0.0) as already
    covered."""
    fake = _FakeVolume()
    target = _mounted_target(tmp_path, monkeypatch, fake)
    target.local_path = tmp_path / "missing.json"

    assert target.exists() is False
    assert fake.reload_count == 1

    assert target.exists() is False
    assert fake.reload_count == 2


# ---------------------------------------------------------------------------
# The fence assignment is thread-safe and monotonic (Copilot #389).
# ---------------------------------------------------------------------------


def test_begin_observation_is_thread_safe_and_monotonic(
    monkeypatch: pytest.MonkeyPatch,
):
    """Two walks can begin concurrently on different threads. The older
    walk's write must not land after the newer one's and move the fence
    backwards, even though it computed an earlier timestamp."""
    from stardag.target import _freshness

    monkeypatch.setattr(_freshness, "_fence", 0.0)

    def fake_monotonic() -> float:
        if threading.current_thread().name == "older":
            # Delay *after* computing the timestamp but before the older
            # walk reaches the lock, so the newer walk finishes first.
            time.sleep(0.2)
            return 100.0
        return 200.0

    monkeypatch.setattr(_freshness.time, "monotonic", fake_monotonic)

    results: dict[str, float] = {}

    older = threading.Thread(
        target=lambda: results.__setitem__("older", _freshness.begin_observation()),
        name="older",
    )
    newer = threading.Thread(
        target=lambda: results.__setitem__("newer", _freshness.begin_observation()),
        name="newer",
    )

    older.start()
    time.sleep(0.05)  # let the older thread enter its monotonic() delay first
    newer.start()
    older.join(timeout=5)
    newer.join(timeout=5)

    assert results["newer"] == 200.0
    # The older walk's own return value reflects the monotonic fence at the
    # time it finished -- i.e. it must observe the newer walk's value, not
    # its own smaller one.
    assert results["older"] == 200.0
    assert _freshness.observation_fence() == 200.0


# ---------------------------------------------------------------------------
# A later walk re-observes completion rather than reusing a prior walk's
# answer (Copilot #389; S28: a yielded child already COMPLETED whose target
# is missing).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_walk_with_a_prior_re_observes_a_deleted_target(
    default_in_memory_fs_target,
):
    import stardag as sd
    from stardag.build._registration import walk_aio

    @sd.task
    def prior_child(x: int) -> int:
        return x

    child = prior_child(x=7)
    child._save(7)
    first = await walk_aio(child, check_stability=False)
    assert first.complete[child.id] is True

    # The target goes away between the build's walk and a yield's.
    default_in_memory_fs_target.clear_targets()
    second = await walk_aio(child, check_stability=False, prior=first)
    assert second.complete[child.id] is False, (
        "The yield's walk reused the prior walk's 'complete' and so could "
        "never report the target missing."
    )
    assert second.observed_at[child.id] > first.observed_at[child.id]
