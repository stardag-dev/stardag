# Backward-compatible re-export. Prefer importing from stardag.testing directly.
from stardag.testing._env import temp_env_vars

__all__ = ["temp_env_vars"]
