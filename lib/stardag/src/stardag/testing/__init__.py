import json
import tempfile
import typing
from contextlib import contextmanager
from pathlib import Path

from stardag.registry import NoOpRegistry, RegistryABC, registry_provider
from stardag.target._factory import _target_roots_override
from stardag.testing._env import temp_env_vars

__all__ = [
    "temp_env_vars",
    "target_roots_override",
    "test_harness",
]


@contextmanager
def target_roots_override(
    target_roots: dict[str, str],
) -> typing.Generator[None, None, None]:
    """Context manager to temporarily override the target roots in the TargetFactory
    and env vars. Env var override is needed so that subprocesses (e.g. in
    multiprocessing) can pick up the new target roots.
    """
    with temp_env_vars({"STARDAG_TARGET_ROOTS": json.dumps(target_roots)}):
        with _target_roots_override(target_roots):
            yield


@contextmanager
def test_harness(
    temp_path: str | Path | None = None,
    registry: RegistryABC | None = None,
    target_root_keys: tuple[str, ...] = ("default",),
) -> typing.Generator[tuple[dict[str, str], RegistryABC], None, None]:
    """Context manager that sets up an isolated test environment.

    Overrides target roots (to temp directories) and the registry provider
    so that tests don't trigger config loading or API calls.

    Args:
        temp_path: Base directory for target roots. If None, a temporary
            directory is created and cleaned up on exit.
        registry: Registry to use. Defaults to NoOpRegistry.
        target_root_keys: Keys for target roots. A subdirectory is created
            for each key under temp_path.

    Yields:
        (target_roots, registry) tuple.
    """
    if registry is None:
        registry = NoOpRegistry()

    def _run(
        base_path: Path,
    ) -> typing.Generator[tuple[dict[str, str], RegistryABC], None, None]:
        target_roots = {}
        for key in target_root_keys:
            root_dir = base_path / key
            root_dir.mkdir(parents=True, exist_ok=True)
            target_roots[key] = str(root_dir)

        with registry_provider.override(registry):
            with target_roots_override(target_roots):
                yield target_roots, registry

    if temp_path is not None:
        yield from _run(Path(temp_path))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            yield from _run(Path(tmp))
