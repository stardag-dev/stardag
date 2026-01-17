"""Fixtures for markdown-docs tests of docstrings (hence must be outside of
src/stardag)."""

import os
import typing
from pathlib import Path

import pytest

from stardag.target import (
    InMemoryFileSystemTarget,
    LocalTarget,
    target_factory_provider,
)
from stardag.target._factory import TargetFactory
from stardag.utils.testing.env import temp_env_vars


@pytest.fixture(scope="function")
def default_local_target_tmp_path(
    tmp_path: Path,
) -> typing.Generator[Path, None, None]:
    import json

    default_root = tmp_path.absolute() / "default-root"
    default_root.mkdir(parents=True, exist_ok=False)

    # Set env var so subprocesses (multiprocessing) can pick it up
    target_roots = {"default": str(default_root)}
    with temp_env_vars({"STARDAG_TARGET_ROOTS": json.dumps(target_roots)}):
        with target_factory_provider.override(
            TargetFactory(
                target_roots=target_roots,
                prefixt_to_target_prototype={"/": LocalTarget},
            )
        ):
            yield default_root


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
            prefixt_to_target_prototype={
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


@pytest.fixture(scope="function", autouse=True)
def cleared_stardag_env_vars() -> typing.Generator[None, None, None]:
    """Clear STARDAG_* environment variables for the duration of the test."""
    stardag_env_vars = [var for var in os.environ if var.startswith("STARDAG_")]
    with temp_env_vars({var: None for var in stardag_env_vars}):
        yield
