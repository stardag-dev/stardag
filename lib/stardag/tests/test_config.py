"""Tests for stardag.config module."""

import json
from pathlib import Path

import pytest

from stardag.config import (
    DEFAULT_API_TIMEOUT,
    DEFAULT_API_URL,
    DEFAULT_REGISTRY,
    DEFAULT_TARGET_ROOT,
    DEFAULT_TARGET_ROOT_KEY,
    ConfigProvider,
    ProjectConfig,
    ProjectRegistryConfig,
    ProjectWorkspaceConfig,
    StardagConfig,
    clear_config_cache,
    find_project_config,
    get_config,
    load_active_registry,
    load_active_workspace,
    load_config,
    load_workspace_target_roots,
    save_active_registry,
    save_active_workspace,
    save_workspace_target_roots,
)


@pytest.fixture
def temp_stardag_dir(tmp_path, monkeypatch):
    """Create a temporary stardag directory and patch Path.home()."""
    stardag_dir = tmp_path / ".stardag"
    stardag_dir.mkdir()

    # Patch Path.home() to return tmp_path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Clear the config cache
    clear_config_cache()

    return stardag_dir


@pytest.fixture
def temp_project_dir(tmp_path, monkeypatch):
    """Create a temporary project directory with .stardag/config.json."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    stardag_config_dir = project_dir / ".stardag"
    stardag_config_dir.mkdir()

    # Change working directory to the project
    monkeypatch.chdir(project_dir)

    return project_dir


class TestLoadActiveRegistry:
    def test_returns_default_when_no_file(self, temp_stardag_dir):
        assert load_active_registry() == DEFAULT_REGISTRY

    def test_returns_saved_registry(self, temp_stardag_dir):
        save_active_registry("production")
        assert load_active_registry() == "production"

    def test_strips_whitespace(self, temp_stardag_dir):
        active_registry_path = temp_stardag_dir / "active_registry"
        active_registry_path.write_text("  my-registry  \n")
        assert load_active_registry() == "my-registry"


class TestSaveActiveRegistry:
    def test_creates_directory_if_needed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Directory doesn't exist yet
        assert not (tmp_path / ".stardag").exists()

        save_active_registry("test-registry")

        assert (tmp_path / ".stardag" / "active_registry").exists()
        assert (
            tmp_path / ".stardag" / "active_registry"
        ).read_text() == "test-registry"


class TestActiveWorkspace:
    def test_returns_none_when_no_file(self, temp_stardag_dir):
        assert load_active_workspace("local") is None

    def test_returns_saved_workspace(self, temp_stardag_dir):
        save_active_workspace("local", "ws-123")
        assert load_active_workspace("local") == "ws-123"

    def test_creates_registry_dir_if_needed(self, temp_stardag_dir):
        # Registry directory doesn't exist yet
        assert not (temp_stardag_dir / "registries" / "newregistry").exists()

        save_active_workspace("newregistry", "ws-456")

        assert (
            temp_stardag_dir / "registries" / "newregistry" / "active_workspace"
        ).exists()
        assert load_active_workspace("newregistry") == "ws-456"


class TestWorkspaceTargetRoots:
    def test_returns_empty_when_no_file(self, temp_stardag_dir):
        result = load_workspace_target_roots("local", "ws-123")
        assert result == {}

    def test_saves_and_loads_target_roots(self, temp_stardag_dir):
        target_roots = {"default": "/data/root", "archive": "s3://bucket/archive"}
        save_workspace_target_roots("local", "ws-123", target_roots)
        result = load_workspace_target_roots("local", "ws-123")
        assert result == target_roots

    def test_loads_from_registry_only(self, temp_stardag_dir):
        """load_workspace_target_roots only loads from registry, not project config."""
        # Save to registry
        registry_roots = {"default": "/registry/root"}
        save_workspace_target_roots("local", "ws-123", registry_roots)

        result = load_workspace_target_roots("local", "ws-123")
        assert result == registry_roots


class TestProjectConfig:
    def test_nested_structure(self):
        """Test nested registries/workspaces structure."""
        config = ProjectConfig(
            default_registry="central",
            allowed_organizations=["my-org"],
            registries={
                "local": ProjectRegistryConfig(
                    organization_id="local-org",
                    default_workspace="dev",
                    workspaces={
                        "dev": ProjectWorkspaceConfig(
                            target_roots={"default": "/local/data"}
                        ),
                    },
                ),
                "central": ProjectRegistryConfig(
                    organization_id="central-org",
                    default_workspace="prod",
                    workspaces={
                        "prod": ProjectWorkspaceConfig(
                            target_roots={"default": "s3://bucket/prod/"}
                        ),
                        "staging": ProjectWorkspaceConfig(
                            target_roots={"default": "s3://bucket/staging/"}
                        ),
                    },
                ),
            },
        )

        assert config.default_registry == "central"

        # Local registry
        assert config.get_organization_id("local") == "local-org"
        assert config.get_workspace_id("local") == "dev"
        assert config.get_workspace_target_roots("local", "dev") == {
            "default": "/local/data"
        }

        # Central registry
        assert config.get_organization_id("central") == "central-org"
        assert config.get_workspace_id("central") == "prod"
        assert config.get_workspace_target_roots("central", "prod") == {
            "default": "s3://bucket/prod/"
        }
        assert config.get_workspace_target_roots("central", "staging") == {
            "default": "s3://bucket/staging/"
        }

    def test_returns_none_for_unknown_registry(self):
        """Test that unknown registry returns None."""
        config = ProjectConfig(
            registries={
                "known-registry": ProjectRegistryConfig(
                    organization_id="known-org",
                )
            },
        )

        assert config.get_organization_id("unknown-registry") is None
        assert config.get_workspace_id("unknown-registry") is None
        assert config.get_workspace_target_roots("unknown-registry", "ws") is None


class TestFindProjectConfig:
    def test_finds_config_in_current_dir(self, temp_project_dir):
        config_path = temp_project_dir / ".stardag" / "config.json"
        config_path.write_text("{}")

        found = find_project_config()
        assert found == config_path

    def test_finds_config_in_parent_dir(self, temp_project_dir, monkeypatch):
        config_path = temp_project_dir / ".stardag" / "config.json"
        config_path.write_text("{}")

        # Create and change to subdirectory
        subdir = temp_project_dir / "src" / "module"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        found = find_project_config()
        assert found == config_path

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert find_project_config() is None


class TestLoadConfig:
    def test_returns_defaults_when_no_config_files(self, temp_stardag_dir, monkeypatch):
        # Ensure no project config
        monkeypatch.chdir(temp_stardag_dir.parent)

        config = load_config(use_project_config=False)

        assert config.target.roots == {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
        assert config.api.url == DEFAULT_API_URL
        assert config.api.timeout == DEFAULT_API_TIMEOUT
        assert config.context.registry == DEFAULT_REGISTRY
        assert config.context.organization_id is None
        assert config.context.workspace_id is None
        assert config.access_token is None
        assert config.api_key is None

    def test_loads_from_registry_config(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        # Create registry config
        registry_dir = temp_stardag_dir / "registries" / "local"
        registry_dir.mkdir(parents=True)
        config_path = registry_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "api_url": "http://my-api:9000",
                    "api_timeout": 60.0,
                    "organization_id": "org-123",
                }
            )
        )

        # Create active_workspace file
        (registry_dir / "active_workspace").write_text("ws-456")

        # Create workspace-specific target roots
        ws_dir = registry_dir / "workspaces" / "ws-456"
        ws_dir.mkdir(parents=True)
        (ws_dir / "target_roots.json").write_text(
            json.dumps({"default": "/custom/path"})
        )

        config = load_config(use_project_config=False)

        assert config.api.url == "http://my-api:9000"
        assert config.api.timeout == 60.0
        assert config.context.organization_id == "org-123"
        assert config.context.workspace_id == "ws-456"
        assert config.target.roots == {"default": "/custom/path"}

    def test_loads_credentials_from_registry(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        # Create registry credentials
        registry_dir = temp_stardag_dir / "registries" / "local"
        registry_dir.mkdir(parents=True)
        creds_path = registry_dir / "credentials.json"
        creds_path.write_text(
            json.dumps(
                {
                    "access_token": "my-jwt-token",
                    "refresh_token": "my-refresh-token",
                }
            )
        )

        config = load_config(use_project_config=False)

        assert config.access_token == "my-jwt-token"

    def test_env_vars_override_registry_config(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        # Create registry config
        registry_dir = temp_stardag_dir / "registries" / "local"
        registry_dir.mkdir(parents=True)
        config_path = registry_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "api_url": "http://registry-api:9000",
                }
            )
        )

        # Create active_workspace file
        (registry_dir / "active_workspace").write_text("registry-ws")

        # Set env vars (should override)
        monkeypatch.setenv("STARDAG_API_URL", "http://env-api:8080")
        monkeypatch.setenv("STARDAG_WORKSPACE_ID", "env-ws")

        clear_config_cache()
        config = load_config(use_project_config=False)

        assert config.api.url == "http://env-api:8080"
        assert config.context.workspace_id == "env-ws"

    def test_project_config_overrides_registry(
        self, temp_stardag_dir, temp_project_dir, monkeypatch
    ):
        # Create registry config
        registry_dir = temp_stardag_dir / "registries" / "local"
        registry_dir.mkdir(parents=True)
        registry_config = registry_dir / "config.json"
        registry_config.write_text(
            json.dumps(
                {
                    "organization_id": "registry-org",
                }
            )
        )
        # Create active_workspace file for registry
        (registry_dir / "active_workspace").write_text("registry-ws")

        # Create project config (overrides organization and workspace via nested structure)
        project_config = temp_project_dir / ".stardag" / "config.json"
        project_config.write_text(
            json.dumps(
                {
                    "default_registry": "local",
                    "registries": {
                        "local": {
                            "organization_id": "project-org",
                            "default_workspace": "project-ws",
                        }
                    },
                }
            )
        )

        config = load_config()

        assert config.context.organization_id == "project-org"
        assert config.context.workspace_id == "project-ws"

    def test_nested_project_config_with_target_roots(
        self, temp_stardag_dir, temp_project_dir, monkeypatch
    ):
        """Test that nested project config provides target roots."""
        # Create registry config (will be used if project config doesn't have it)
        registry_dir = temp_stardag_dir / "registries" / "local"
        registry_dir.mkdir(parents=True)
        registry_config = registry_dir / "config.json"
        registry_config.write_text(json.dumps({}))

        # Create nested project config with target roots
        project_config = temp_project_dir / ".stardag" / "config.json"
        project_config.write_text(
            json.dumps(
                {
                    "default_registry": "local",
                    "registries": {
                        "local": {
                            "organization_id": "my-org",
                            "default_workspace": "dev-ws",
                            "workspaces": {
                                "dev-ws": {
                                    "target_roots": {"default": "/project/dev/data"}
                                }
                            },
                        }
                    },
                }
            )
        )

        config = load_config()

        assert config.context.registry == "local"
        assert config.context.organization_id == "my-org"
        assert config.context.workspace_id == "dev-ws"
        assert config.target.roots == {"default": "/project/dev/data"}

    def test_project_config_can_set_registry(self, temp_stardag_dir, temp_project_dir):
        # Create production registry config
        prod_registry_dir = temp_stardag_dir / "registries" / "production"
        prod_registry_dir.mkdir(parents=True)
        prod_config = prod_registry_dir / "config.json"
        prod_config.write_text(
            json.dumps(
                {
                    "api_url": "https://api.stardag.io",
                }
            )
        )

        # Create project config that selects production registry
        project_config = temp_project_dir / ".stardag" / "config.json"
        project_config.write_text(
            json.dumps(
                {
                    "default_registry": "production",
                }
            )
        )

        config = load_config()

        assert config.context.registry == "production"
        assert config.api.url == "https://api.stardag.io"

    def test_api_key_from_env_var(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)
        monkeypatch.setenv("STARDAG_API_KEY", "my-api-key")

        clear_config_cache()
        config = load_config(use_project_config=False)

        assert config.api_key == "my-api-key"

    def test_allowed_organizations_from_project_config(
        self, temp_stardag_dir, temp_project_dir
    ):
        project_config = temp_project_dir / ".stardag" / "config.json"
        project_config.write_text(
            json.dumps(
                {
                    "allowed_organizations": ["org-1", "org-2"],
                }
            )
        )

        config = load_config()

        assert config.allowed_organizations == ["org-1", "org-2"]


class TestStardagConfigValidation:
    def test_validate_organization_passes_when_allowed(self):
        config = StardagConfig(allowed_organizations=["org-1", "org-2"])
        config.validate_organization("org-1")  # Should not raise

    def test_validate_organization_raises_when_not_allowed(self):
        config = StardagConfig(allowed_organizations=["org-1", "org-2"])
        with pytest.raises(ValueError, match="not allowed"):
            config.validate_organization("org-3")

    def test_validate_organization_passes_when_no_restrictions(self):
        config = StardagConfig()
        config.validate_organization("any-org")  # Should not raise

    def test_validate_organization_passes_when_slug_matches(self):
        """Test that validation passes when org_slug matches allowed list."""
        from stardag.config import ContextConfig

        config = StardagConfig(
            context=ContextConfig(
                organization_id="some-uuid-id",
                organization_slug="my-org-slug",
            ),
            allowed_organizations=["my-org-slug", "other-org"],
        )
        # ID doesn't match, but slug does - should pass
        config.validate_organization()  # Should not raise


class TestConfigProvider:
    def test_get_returns_loaded_config(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        provider = ConfigProvider()
        config = provider.get()

        assert isinstance(config, StardagConfig)

    def test_set_overrides_config(self):
        from stardag.config import APIConfig

        provider = ConfigProvider()
        custom_config = StardagConfig(
            api=APIConfig(url="http://custom:1234", timeout=99.0)
        )

        provider.set(custom_config)

        assert provider.get().api.url == "http://custom:1234"
        assert provider.get().api.timeout == 99.0

    def test_reset_clears_override(self, temp_stardag_dir, monkeypatch):
        from stardag.config import APIConfig

        monkeypatch.chdir(temp_stardag_dir.parent)

        provider = ConfigProvider()
        custom_config = StardagConfig(
            api=APIConfig(url="http://custom:1234", timeout=99.0)
        )

        provider.set(custom_config)
        assert provider.get().api.url == "http://custom:1234"

        provider.reset()
        assert provider.get().api.url == DEFAULT_API_URL


class TestGetConfig:
    def test_caches_result(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)
        clear_config_cache()

        config1 = get_config()
        config2 = get_config()

        assert config1 is config2

    def test_clear_cache_forces_reload(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)
        clear_config_cache()

        config1 = get_config()
        clear_config_cache()
        config2 = get_config()

        # After clearing cache, should be a new instance
        assert config1 is not config2
