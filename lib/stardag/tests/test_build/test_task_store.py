"""Tests for BuildTaskStore write-once semantics.

The store lives on a target root that may be immutable/append-only, so its
writes must never require overwriting an existing object (this is what
broke re-triggering a reactive build on a Modal volume).
"""

from __future__ import annotations

import typing
from uuid import uuid4

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


def test_load_missing_task_returns_none(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    # The store is pickle-only now (orchestration metadata lives in the
    # registry); a never-persisted task id loads as None.
    store = BuildTaskStore(uuid4())
    assert store.load_task(uuid4()) is None
