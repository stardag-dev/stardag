import os
import typing
from pathlib import Path

import pytest

from stardag.target import (
    InMemoryFileSystemTarget,
    target_factory_provider,
)
from stardag.target._factory import TargetFactory
from stardag.testing import target_roots_override, temp_env_vars
from stardag.utils.testing.simple_dag import (
    get_simple_dag,
    get_simple_dag_expected_root_output,
)


# Register custom markers
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test (may require external services)",
    )


@pytest.fixture(scope="session")
def simple_dag():
    return get_simple_dag()


@pytest.fixture(scope="session")
def simple_dag_expected_root_output():
    return get_simple_dag_expected_root_output()


@pytest.fixture(scope="function")
def default_local_target_tmp_path(
    tmp_path: Path,
) -> typing.Generator[Path, None, None]:
    default_root = tmp_path.absolute() / "default-root"
    default_root.mkdir(parents=True, exist_ok=False)

    # NOTE sets env var so subprocesses (multiprocessing) can pick it up
    target_roots = {"default": str(default_root)}
    with target_roots_override(target_roots):
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


@pytest.fixture(scope="function", autouse=True)
def cleared_stardag_env_vars() -> typing.Generator[None, None, None]:
    """Clear STARDAG_* environment variables for the duration of the test."""
    stardag_env_vars = [var for var in os.environ if var.startswith("STARDAG_")]
    with temp_env_vars({var: None for var in stardag_env_vars}):
        yield
