import os
from collections.abc import Mapping
from contextlib import contextmanager

__all__ = ["temp_env_vars"]


@contextmanager
def temp_env_vars(name_value: Mapping[str, str | None]):
    """Temporarily set or unset environment variables within a context.

    On exit the original environment is restored (variables that were unset
    before entering are unset again; variables that had a value are restored
    to that value).

    Used both in unit tests (to isolate env mutations) and at runtime to apply
    per-task environment overrides around a task run.

    Args:
        name_value: Mapping of env var name to value. Use None to temporarily
            unset a variable.
    """
    original = {name: os.getenv(name, None) for name in name_value}
    for name, value in name_value.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name in name_value:
            if original[name] is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original[name]  # type: ignore
