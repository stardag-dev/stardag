"""Tests for stardag.testing utilities."""

from __future__ import annotations

from pathlib import Path

from stardag.registry import NoOpRegistry, registry_provider
from stardag.target._factory import target_factory_provider
from stardag.testing import test_harness


class TestTestHarness:
    """Tests for the test_harness context manager."""

    def test_creates_temp_dirs_for_default_key(self):
        with test_harness() as (target_roots, reg):
            assert "default" in target_roots
            assert Path(target_roots["default"]).is_dir()

    def test_temp_dirs_cleaned_up_after_exit(self):
        with test_harness() as (target_roots, _):
            default_path = Path(target_roots["default"])
            assert default_path.is_dir()
        assert not default_path.exists()

    def test_custom_target_root_keys(self):
        keys = ("default", "archive", "staging")
        with test_harness(target_root_keys=keys) as (target_roots, _):
            assert set(target_roots.keys()) == set(keys)
            for key in keys:
                assert Path(target_roots[key]).is_dir()
                assert Path(target_roots[key]).name == key

    def test_uses_noop_registry_by_default(self):
        with test_harness() as (_, reg):
            assert isinstance(reg, NoOpRegistry)

    def test_custom_registry(self):
        custom = NoOpRegistry()
        with test_harness(registry=custom) as (_, reg):
            assert reg is custom

    def test_registry_provider_overridden(self):
        with test_harness() as (_, reg):
            assert registry_provider.get() is reg

    def test_target_factory_provider_overridden(self):
        with test_harness() as (target_roots, _):
            factory = target_factory_provider.get()
            assert factory.target_roots == {
                k: v.removesuffix("/") + "/" for k, v in target_roots.items()
            }

    def test_providers_restored_after_exit(self):
        original_factory_initialized = target_factory_provider.is_initialized()
        original_registry_initialized = registry_provider.is_initialized()

        with test_harness():
            pass

        assert target_factory_provider.is_initialized() == original_factory_initialized
        assert registry_provider.is_initialized() == original_registry_initialized

    def test_explicit_temp_path(self, tmp_path: Path):
        with test_harness(temp_path=tmp_path) as (target_roots, _):
            assert Path(target_roots["default"]).parent == tmp_path
        # Explicit path is NOT cleaned up
        assert tmp_path.exists()
