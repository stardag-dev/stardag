"""Pytest fixtures for stardag-examples tests."""

import typing

import pytest
from stardag.target import (
    InMemoryFileSystemTarget,
    target_factory_provider,
)
from stardag.target._factory import TargetFactory


@pytest.fixture(scope="session")
def default_in_memory_fs_target_prefix():
    return "in-memory://"


@pytest.fixture(scope="function")
def _default_in_memory_fs_target_factory(
    default_in_memory_fs_target_prefix,
) -> typing.Generator[TargetFactory, None, None]:
    with target_factory_provider.override(
        TargetFactory(
            target_roots={"default": default_in_memory_fs_target_prefix},
            prefix_to_target_prototype={
                default_in_memory_fs_target_prefix: InMemoryFileSystemTarget
            },
        )
    ) as target_factory:
        with InMemoryFileSystemTarget.cleared():
            yield target_factory


@pytest.fixture(scope="function")
def default_in_memory_fs_target(
    _default_in_memory_fs_target_factory,
) -> typing.Type[InMemoryFileSystemTarget]:
    return InMemoryFileSystemTarget
