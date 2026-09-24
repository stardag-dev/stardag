"""One instance per task id per plan, in every discovery walk: two
constructions of one task id with different instance hashes raise
``InstanceConflictError`` naming both construction paths. Design:
docs/design/registry-v2/design.md, "Two hashes, one flag"."""

from __future__ import annotations

import typing
from typing import Annotated

import pytest

import stardag as sd
from stardag import InstanceConflictError, StardagField
from stardag.build import (
    build_aio,
    build_sequential,
    build_sequential_aio,
)
from stardag.build._registration import walk_aio
from stardag.target import InMemoryFileTarget


class ConflictLeaf(sd.Task[int]):
    __namespace__ = "instance_conflict_tests"

    key: str
    width: Annotated[int, StardagField(significant=False)] = 1

    def run(self) -> None:
        self._save(self.width)


class ConflictMid(sd.Task[int]):
    __namespace__ = "instance_conflict_tests"

    label: str
    up: sd.TaskLoads[int]

    def requires(self):  # type: ignore[override]
        return self.up

    def run(self) -> None:
        self._save(0)


class ConflictRoot(sd.Task[int]):
    __namespace__ = "instance_conflict_tests"

    ups: tuple[sd.TaskLoads[int], ...]

    def requires(self):  # type: ignore[override]
        return self.ups

    def run(self) -> None:
        self._save(0)


class YieldingRoot(sd.Task[int]):
    """Requires ``static`` and yields ``dynamic`` from ``run``."""

    __namespace__ = "instance_conflict_tests"

    static: sd.TaskLoads[int]
    dynamic: sd.TaskLoads[int]

    def requires(self):  # type: ignore[override]
        return self.static

    def run(self):  # type: ignore[override]
        yield self.dynamic
        self._save(0)


def _static_conflict() -> ConflictRoot:
    return ConflictRoot(
        ups=(
            ConflictMid(label="a", up=ConflictLeaf(key="x", width=1)),
            ConflictMid(label="b", up=ConflictLeaf(key="x", width=2)),
        )
    )


def _dynamic_conflict() -> YieldingRoot:
    return YieldingRoot(
        static=ConflictLeaf(key="y", width=1),
        dynamic=ConflictLeaf(key="y", width=2),
    )


def _the_conflict(
    excinfo: pytest.ExceptionInfo[BaseException],
) -> InstanceConflictError:
    error = excinfo.value
    # A TaskGroup-based walk may wrap it in an exception group.
    while not isinstance(error, InstanceConflictError) and getattr(
        error, "exceptions", None
    ):
        error = error.exceptions[0]  # type: ignore[attr-defined]
    assert isinstance(error, InstanceConflictError), repr(excinfo.value)
    return error


def _assert_static_paths(error: InstanceConflictError) -> None:
    assert error.fields == ("width",)
    first, second = error.paths
    assert first is not None and second is not None
    # root first, the conflicting task last, via the two different mids
    for path in (first, second):
        steps = path.split(" -> ")
        assert steps[0].startswith("ConflictRoot[")
        assert steps[1].startswith("ConflictMid[")
        assert steps[2].startswith("ConflictLeaf[")
    assert first.split(" -> ")[1] != second.split(" -> ")[1]
    assert first in str(error) and second in str(error)


def _assert_dynamic_paths(error: InstanceConflictError) -> None:
    assert error.fields == ("width",)
    first, second = error.paths
    assert first is not None and second is not None
    for path in (first, second):
        steps = path.split(" -> ")
        assert [s.split("[")[0] for s in steps] == ["YieldingRoot", "ConflictLeaf"]


@pytest.fixture
def _memory(default_in_memory_fs_target: typing.Type[InMemoryFileTarget]):
    return default_in_memory_fs_target


@pytest.mark.usefixtures("_memory")
class TestStaticConflict:
    def test_build_sequential(self, noop_registry):
        with pytest.raises(BaseException) as excinfo:
            build_sequential(_static_conflict(), registry=noop_registry)
        _assert_static_paths(_the_conflict(excinfo))

    async def test_build_sequential_aio(self, noop_registry):
        with pytest.raises(BaseException) as excinfo:
            await build_sequential_aio(_static_conflict(), registry=noop_registry)
        _assert_static_paths(_the_conflict(excinfo))

    async def test_build_aio(self, noop_registry):
        with pytest.raises(BaseException) as excinfo:
            await build_aio(_static_conflict(), registry=noop_registry)
        _assert_static_paths(_the_conflict(excinfo))

    async def test_the_walk_every_driver_shares(self):
        """The bootstrap, a tick's discovery job and a worker's yield walk
        with the same function the resident engines do."""
        with pytest.raises(BaseException) as excinfo:
            await walk_aio(_static_conflict())
        _assert_static_paths(_the_conflict(excinfo))

    def test_the_same_instance_twice_is_fine(self, noop_registry):
        leaf = ConflictLeaf(key="z", width=3)
        root = ConflictRoot(
            ups=(
                ConflictMid(label="a", up=leaf),
                ConflictMid(label="b", up=ConflictLeaf(key="z", width=3)),
            )
        )
        build_sequential(root, registry=noop_registry)
        assert root.complete()


@pytest.mark.usefixtures("_memory")
class TestDynamicConflict:
    """A yielded dep conflicting with a statically planned one."""

    def test_build_sequential(self, noop_registry):
        with pytest.raises(BaseException) as excinfo:
            build_sequential(_dynamic_conflict(), registry=noop_registry)
        _assert_dynamic_paths(_the_conflict(excinfo))

    async def test_build_sequential_aio(self, noop_registry):
        with pytest.raises(BaseException) as excinfo:
            await build_sequential_aio(_dynamic_conflict(), registry=noop_registry)
        _assert_dynamic_paths(_the_conflict(excinfo))

    async def test_build_aio(self, noop_registry):
        with pytest.raises(BaseException) as excinfo:
            await build_aio(_dynamic_conflict(), registry=noop_registry)
        _assert_dynamic_paths(_the_conflict(excinfo))
