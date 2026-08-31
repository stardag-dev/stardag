"""Tests for BuildTaskStore write-once semantics.

The store lives on a target root that may be immutable/append-only, so its
writes must never require overwriting an existing object (this is what
broke re-triggering a reactive build on a Modal volume).
"""

from __future__ import annotations

import logging
import typing
from uuid import uuid4

import pytest

from stardag.build import BuildTaskStore
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask


def test_save_task_is_idempotent(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    store = BuildTaskStore(uuid4())
    task = SyncOnlyTask(name="ws")
    store.save_task(task)
    # Saving the same task id again must be a no-op, not an overwrite/error.
    store.save_task(task)
    loaded = store.load_task(task.id)
    assert loaded is not None
    assert loaded.id == task.id


def test_save_tasks_skips_already_persisted(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    store = BuildTaskStore(uuid4())
    a, b = SyncOnlyTask(name="a"), SyncOnlyTask(name="b")
    store.save_tasks([a])
    # A re-trigger re-persisting a DAG that overlaps the first must not fail.
    store.save_tasks([a, b])
    assert store.load_task(a.id) is not None
    assert store.load_task(b.id) is not None


async def test_save_task_aio_is_idempotent(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    store = BuildTaskStore(uuid4())
    task = SyncOnlyTask(name="ws")
    await store.save_task_aio(task)
    await store.save_task_aio(task)
    loaded = await store.load_task_aio(task.id)
    assert loaded is not None
    assert loaded.id == task.id


async def test_sync_and_aio_paths_share_the_store(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    # The aio variants exist so loop-based callers (the reactive tick) avoid
    # blocking I/O on the event loop — they must read/write the very same
    # entries as the sync path used by the trigger and workers.
    store = BuildTaskStore(uuid4())
    a, b = SyncOnlyTask(name="a"), SyncOnlyTask(name="b")
    store.save_task(a)
    await store.save_tasks_aio([a, b])  # write-once: overlap must not fail
    loaded_a = await store.load_task_aio(a.id)
    assert loaded_a is not None and loaded_a.id == a.id
    loaded_b = store.load_task(b.id)
    assert loaded_b is not None and loaded_b.id == b.id


async def test_load_task_aio_missing_returns_none(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    store = BuildTaskStore(uuid4())
    assert await store.load_task_aio(uuid4()) is None


def test_load_missing_task_returns_none(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    # The store is pickle-only now (orchestration metadata lives in the
    # registry); a never-persisted task id loads as None.
    store = BuildTaskStore(uuid4())
    assert store.load_task(uuid4()) is None


def test_load_missing_task_does_not_log_an_error(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    caplog: pytest.LogCaptureFixture,
):
    # A miss is the normal path once pickles are elided (declared
    # `task_modules`), and `_load_task` rehydrates from registry data. Only
    # that caller can tell a real failure from a routine miss, so the store
    # must not report one — nothing at WARNING or above. An ERROR here
    # trained readers to ignore the scheduler's error lines on the
    # recommended configuration. DEBUG is still expected and asserted: the
    # miss stays traceable for anyone debugging a rehydration failure.
    store = BuildTaskStore(uuid4())
    logger_name = "stardag.build._task_store"
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        assert store.load_task(uuid4()) is None
    # Scoped to the store's own logger: `at_level` raises the level for that
    # logger only, but `caplog.records` captures whatever any logger emits,
    # so an unrelated WARNING from the target factory would otherwise fail a
    # test that is not about it.
    records = [r for r in caplog.records if r.name == logger_name]
    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert any(r.levelno == logging.DEBUG for r in records)
