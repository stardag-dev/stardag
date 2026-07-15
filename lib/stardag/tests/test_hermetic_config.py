"""Regression tests for hermetic test configuration.

The unit suite must never read the developer's ambient stardag configuration
(user/project ``config.toml``, active profile, or ``STARDAG_*`` env vars). A
machine-local *remote* default target root (e.g. an ``s3://`` / cloud-volume
root) used to leak into any test that built ``task.target()`` without its own
target-isolation fixture, failing it with a "URI ... does not match any of the
configured prefixes" error. The autouse ``hermetic_config`` fixture in
``conftest.py`` guarantees a local, offline baseline instead.
"""

import stardag as sd
from stardag import LocalFileTarget
from stardag.config import config_provider
from stardag.target.serialize import FileSerializable


class _HermeticIntTask(sd.Task[int]):
    value: int = 0

    def run(self):
        self._save(self.value)


def test_config_is_offline_by_default():
    """No ambient registry leaks into the suite."""
    assert config_provider.get().registry is None


def test_default_target_root_is_local():
    """The default target root is a local filesystem path, not a remote scheme
    inherited from the developer's config."""
    default_root = config_provider.get().target.roots["default"]
    assert "://" not in default_root


def test_target_builds_local_regardless_of_ambient_config():
    """Building a task target succeeds and yields a local file target — the
    exact path that broke when ambient config supplied a remote root."""
    target = _HermeticIntTask(value=1).target()
    assert isinstance(target, FileSerializable)
    assert isinstance(target.wrapped, LocalFileTarget)
