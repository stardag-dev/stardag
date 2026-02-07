"""Tests for stardag.config module."""

from pathlib import Path

import pytest

from stardag.config import (
    DEFAULT_API_TIMEOUT,
    DEFAULT_API_URL,
    DEFAULT_TARGET_ROOT,
    DEFAULT_TARGET_ROOT_KEY,
    ContextConfig,
    ProfileConfig,
    RegistryConfig,
    TomlConfig,
    clear_config_cache,
    find_project_config,
    get_config,
    load_config,
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
    """Create a temporary project directory with .stardag/config.toml."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    stardag_config_dir = project_dir / ".stardag"
    stardag_config_dir.mkdir()

    # Change working directory to the project
    monkeypatch.chdir(project_dir)

    return project_dir


def write_toml(path: Path, content: str) -> None:
    """Write a TOML file, handling tomli-w dependency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestTomlConfig:
    def test_parses_empty_dict(self):
        """Test parsing empty config."""
        config = TomlConfig.from_toml_dict({})
        assert config.registry == {}
        assert config.profile == {}
        assert config.default == {}

    def test_parses_registries(self):
        """Test parsing registry configuration."""
        config = TomlConfig.from_toml_dict(
            {
                "registry": {
                    "local": {"url": "http://localhost:8000"},
                    "central": {"url": "https://api.stardag.com"},
                }
            }
        )
        assert "local" in config.registry
        assert config.registry["local"].url == "http://localhost:8000"
        assert config.registry["central"].url == "https://api.stardag.com"

    def test_parses_profiles(self):
        """Test parsing profile configuration."""
        config = TomlConfig.from_toml_dict(
            {
                "profile": {
                    "dev": {
                        "registry": "local",
                        "workspace": "my-workspace",
                        "environment": "dev-env",
                    }
                }
            }
        )
        assert "dev" in config.profile
        assert config.profile["dev"].registry == "local"
        assert config.profile["dev"].workspace == "my-workspace"
        assert config.profile["dev"].environment == "dev-env"

    def test_parses_default(self):
        """Test parsing default profile setting."""
        config = TomlConfig.from_toml_dict({"default": {"profile": "dev"}})
        assert config.default.get("profile") == "dev"


class TestFindProjectConfig:
    def test_finds_config_in_current_dir(self, temp_project_dir):
        config_path = temp_project_dir / ".stardag" / "config.toml"
        write_toml(config_path, "[default]\nprofile = 'local'\n")

        found = find_project_config()
        assert found == config_path

    def test_finds_config_in_parent_dir(self, temp_project_dir, monkeypatch):
        config_path = temp_project_dir / ".stardag" / "config.toml"
        write_toml(config_path, "[default]\nprofile = 'local'\n")

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
        assert config.context.profile is None
        assert config.context.registry_name is None
        assert config.context.workspace_id is None
        assert config.context.environment_id is None
        assert config.access_token is None
        assert config.api_key is None

    def test_loads_from_user_config_toml(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        # Create user config.toml
        config_path = temp_stardag_dir / "config.toml"
        write_toml(
            config_path,
            """
[registry.local]
url = "http://my-api:9000"

[profile.dev]
registry = "local"
workspace = "workspace-123"
environment = "env-456"

[default]
profile = "dev"
""",
        )

        clear_config_cache()
        config = load_config(use_project_config=False)

        assert config.context.profile == "dev"
        assert config.context.registry_name == "local"
        assert config.context.workspace_id == "workspace-123"
        assert config.context.environment_id == "env-456"
        assert config.api.url == "http://my-api:9000"

    def test_env_vars_override_config(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        # Create user config
        config_path = temp_stardag_dir / "config.toml"
        write_toml(
            config_path,
            """
[registry.local]
url = "http://registry-api:9000"

[profile.dev]
registry = "local"
workspace = "config-workspace"
environment = "config-env"

[default]
profile = "dev"
""",
        )

        # Set env vars (should override)
        monkeypatch.setenv("STARDAG_REGISTRY_URL", "http://env-api:8080")
        monkeypatch.setenv("STARDAG_WORKSPACE_ID", "env-workspace")
        monkeypatch.setenv("STARDAG_ENVIRONMENT_ID", "env-env")

        clear_config_cache()
        config = load_config(use_project_config=False)

        # Direct env vars override profile-based config
        assert config.api.url == "http://env-api:8080"
        assert config.context.workspace_id == "env-workspace"
        assert config.context.environment_id == "env-env"

    def test_profile_env_var_selects_profile(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)

        # Create config with multiple profiles
        config_path = temp_stardag_dir / "config.toml"
        write_toml(
            config_path,
            """
[registry.local]
url = "http://localhost:8000"

[registry.prod]
url = "https://api.stardag.com"

[profile.dev]
registry = "local"
workspace = "dev-workspace"
environment = "dev-env"

[profile.prod]
registry = "prod"
workspace = "prod-workspace"
environment = "prod-env"

[default]
profile = "dev"
""",
        )

        # Select prod profile via env var
        monkeypatch.setenv("STARDAG_PROFILE", "prod")

        clear_config_cache()
        config = load_config(use_project_config=False)

        assert config.context.profile == "prod"
        assert config.context.registry_name == "prod"
        assert config.context.workspace_id == "prod-workspace"
        assert config.context.environment_id == "prod-env"
        assert config.api.url == "https://api.stardag.com"

    def test_project_config_overrides_user_config(
        self, temp_stardag_dir, temp_project_dir, monkeypatch
    ):
        # Create user config
        user_config = temp_stardag_dir / "config.toml"
        write_toml(
            user_config,
            """
[registry.local]
url = "http://localhost:8000"

[profile.dev]
registry = "local"
workspace = "user-workspace"
environment = "user-env"

[default]
profile = "dev"
""",
        )

        # Create project config (overrides)
        project_config = temp_project_dir / ".stardag" / "config.toml"
        write_toml(
            project_config,
            """
[profile.dev]
registry = "local"
workspace = "project-workspace"
environment = "project-env"
""",
        )

        clear_config_cache()
        config = load_config()

        # Project config should override user config
        assert config.context.workspace_id == "project-workspace"
        assert config.context.environment_id == "project-env"

    def test_api_key_from_env_var(self, temp_stardag_dir, monkeypatch):
        monkeypatch.chdir(temp_stardag_dir.parent)
        monkeypatch.setenv("STARDAG_API_KEY", "my-api-key")

        clear_config_cache()
        config = load_config(use_project_config=False)

        assert config.api_key == "my-api-key"


class TestContextConfig:
    def test_default_values(self):
        ctx = ContextConfig()
        assert ctx.profile is None
        assert ctx.registry_name is None
        assert ctx.workspace_id is None
        assert ctx.environment_id is None

    def test_with_values(self):
        ctx = ContextConfig(
            profile="dev",
            registry_name="local",
            workspace_id="workspace-123",
            environment_id="env-456",
        )
        assert ctx.profile == "dev"
        assert ctx.registry_name == "local"
        assert ctx.workspace_id == "workspace-123"
        assert ctx.environment_id == "env-456"


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


class TestRegistryConfig:
    def test_default_url(self):
        config = RegistryConfig()
        assert config.url == DEFAULT_API_URL

    def test_custom_url(self):
        config = RegistryConfig(url="https://api.stardag.com")
        assert config.url == "https://api.stardag.com"


class TestProfileConfig:
    def test_required_fields(self):
        config = ProfileConfig(
            registry="local", workspace="my-workspace", environment="dev-env"
        )
        assert config.registry == "local"
        assert config.workspace == "my-workspace"
        assert config.environment == "dev-env"
