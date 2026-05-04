"""Pure configuration tests for the modalvol disk-cache wiring.

These tests only exercise ``_init_modal_volume_file_system()`` — they do
**not** hit the Modal API and do **not** require Modal credentials or a
real volume. Round-trip behaviour against a real volume is covered by
``test__target_cache.py`` (which is gated on auth).
"""

import pytest

try:
    import modal  # noqa: F401  — gate import errors at module level
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.integration.modal._target import (
    ModalVolumeRemoteFileSystem,
    _init_modal_volume_file_system,
)
from stardag.target import CachedRemoteFileSystem


def test_init_unwrapped_when_cache_root_unset(monkeypatch: pytest.MonkeyPatch):
    """No env var → caching is disabled and the bare RFS is returned."""
    monkeypatch.delenv("STARDAG_TARGET_MODALVOL_CACHE_ROOT", raising=False)
    fs = _init_modal_volume_file_system()
    assert isinstance(fs, ModalVolumeRemoteFileSystem)
    assert not isinstance(fs, CachedRemoteFileSystem)


def test_init_wrapped_when_cache_root_set(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Setting STARDAG_TARGET_MODALVOL_CACHE_ROOT enables caching with that
    root — there is intentionally no default root."""
    monkeypatch.setenv("STARDAG_TARGET_MODALVOL_CACHE_ROOT", str(tmp_path))
    fs = _init_modal_volume_file_system()
    assert isinstance(fs, CachedRemoteFileSystem)
    assert isinstance(fs.wrapped, ModalVolumeRemoteFileSystem)
    assert str(fs.root) == str(tmp_path)
