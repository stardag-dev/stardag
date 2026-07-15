import os
import typing
from pathlib import Path

import pytest

from stardag.config import (
    DEFAULT_TARGET_ROOT_KEY,
    StardagConfig,
    TargetConfig,
    config_provider,
)
from stardag.target import (
    InMemoryFileTarget,
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
                default_in_memory_fs_target_prefix: InMemoryFileTarget
            },
        )
    ) as target_factory:
        with InMemoryFileTarget.cleared():
            yield target_factory


@pytest.fixture(scope="function")
def default_in_memory_fs_target(
    _default_in_memory_fs_target_factory,
) -> typing.Type[InMemoryFileTarget]:
    return InMemoryFileTarget


@pytest.fixture(scope="function", autouse=True)
def cleared_stardag_env_vars() -> typing.Generator[None, None, None]:
    """Clear STARDAG_* environment variables for the duration of the test."""
    stardag_env_vars = [var for var in os.environ if var.startswith("STARDAG_")]
    with temp_env_vars({var: None for var in stardag_env_vars}):
        yield


@pytest.fixture(scope="function", autouse=True)
def hermetic_config(
    cleared_stardag_env_vars: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[None, None, None]:
    """Isolate every test from the developer's ambient stardag configuration.

    Config is resolved not only from ``STARDAG_*`` env vars (which
    ``cleared_stardag_env_vars`` already clears) but also from user/project
    ``config.toml`` files and the active profile. Without this, a machine-local
    default target root (e.g. a remote ``s3://`` / cloud-volume root) or registry
    would leak into unit tests — most visibly into tests that build
    ``task.target()`` without their own target-isolation fixture.

    This overrides ``config_provider`` with a hermetic default — offline (no
    registry) and a local temp target root — and rebuilds
    ``target_factory_provider`` from it. Tests that set up their own factory
    (``default_in_memory_fs_target``) or roots (``default_local_target_tmp_path``)
    override on top and restore back to this hermetic baseline. Config-loader
    tests that call ``clear_config_cache()`` and load from their own
    monkeypatched sources are unaffected (the clear drops this override, and it
    is re-established for the next test).
    """
    default_root = tmp_path_factory.mktemp("hermetic-target-root")
    hermetic = StardagConfig(
        registry=None,
        target=TargetConfig(roots={DEFAULT_TARGET_ROOT_KEY: str(default_root)}),
    )
    with config_provider.override(hermetic):
        # Build the factory inside the config override so it reads the hermetic
        # roots, not a factory lazily built from ambient config earlier.
        with target_factory_provider.override(TargetFactory()):
            yield
