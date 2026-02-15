import json
import typing
from contextlib import contextmanager

from stardag.target._factory import _target_roots_override
from stardag.testing._env import temp_env_vars

__all__ = [
    "temp_env_vars",
    "target_roots_override",
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
