import os
from collections.abc import Mapping
from contextlib import contextmanager


@contextmanager
def temp_env_vars(name_value: Mapping[str, str | None]):
    """Temporarily set or unset environment variables within a context.

    E.g. used in unit tests to make sure that the original environment variables are
    restored after exiting the scope.

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
