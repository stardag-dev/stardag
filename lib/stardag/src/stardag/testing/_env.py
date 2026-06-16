# Backward-compatible re-export. The implementation lives in
# ``stardag.utils.env`` so that non-testing runtime code (e.g. the Modal
# integration's per-task env overrides) can use it without importing from a
# ``testing`` module.
from stardag.utils.env import temp_env_vars

__all__ = ["temp_env_vars"]
