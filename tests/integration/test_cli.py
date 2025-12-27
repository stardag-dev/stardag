"""CLI integration tests.

These tests verify the CLI works correctly with the docker-compose services.
Note: Interactive login tests are limited since they require browser interaction.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from docker_fixtures import ServiceEndpoints


def run_stardag_cli(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the stardag CLI with given arguments.

    Uses the lib/stardag package directly via uv run stardag command.
    """
    # Use the lib/stardag directory for running CLI
    stardag_dir = Path(__file__).parent.parent.parent / "lib" / "stardag"

    # Merge environment
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    # Run the CLI via the console script entry point
    cmd = ["uv", "run", "stardag", *args]
    result = subprocess.run(
        cmd,
        cwd=str(cwd or stardag_dir),
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )

    return result


class TestCliBasics:
    """Test basic CLI functionality."""

    def test_cli_version(self) -> None:
        """Test that CLI shows version."""
        result = run_stardag_cli("version")
        assert result.returncode == 0
        assert "stardag" in result.stdout.lower()

    def test_cli_help(self) -> None:
        """Test that CLI shows help."""
        result = run_stardag_cli("--help")
        assert result.returncode == 0
        assert "stardag" in result.stdout.lower()
        assert "auth" in result.stdout.lower()
        assert "config" in result.stdout.lower()

    def test_auth_help(self) -> None:
        """Test that auth subcommand shows help."""
        result = run_stardag_cli("auth", "--help")
        assert result.returncode == 0
        assert "login" in result.stdout.lower()
        assert "logout" in result.stdout.lower()
        assert "status" in result.stdout.lower()

    def test_config_help(self) -> None:
        """Test that config subcommand shows help."""
        result = run_stardag_cli("config", "--help")
        assert result.returncode == 0
        assert "show" in result.stdout.lower()
        assert "registry" in result.stdout.lower()
        assert "profile" in result.stdout.lower()


class TestCliConfigWithoutAuth:
    """Test config commands that work without authentication."""

    def test_config_show_no_profile(self) -> None:
        """Test config show works without any profile set."""
        # Use a clean home directory to avoid interference
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}
            result = run_stardag_cli("config", "show", env=env)
            assert result.returncode == 0
            assert "Configuration:" in result.stdout
            assert "Active Context:" in result.stdout

    def test_auth_status_no_credentials(self) -> None:
        """Test auth status works without credentials."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}
            result = run_stardag_cli("auth", "status", env=env)
            assert result.returncode == 0
            # Should show "not configured" or similar
            assert (
                "Configuration:" in result.stdout or "status" in result.stdout.lower()
            )


class TestCliApiKeyAuth:
    """Test CLI with API key authentication."""

    def test_config_show_with_api_key(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test config show when API key is set."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "CLI Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Run config show with API key
        env = {"STARDAG_API_KEY": api_key}
        result = run_stardag_cli("config", "show", env=env)
        assert result.returncode == 0
        assert "API Key" in result.stdout

    def test_auth_status_with_api_key(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test auth status shows API key is in use."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Status Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Run auth status with API key
        env = {"STARDAG_API_KEY": api_key}
        result = run_stardag_cli("auth", "status", env=env)
        assert result.returncode == 0
        assert "API Key" in result.stdout


class TestCliEnvConfig:
    """Test CLI respects environment variable configuration."""

    def test_registry_url_from_env(
        self,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that STARDAG_REGISTRY_URL is respected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HOME": tmpdir,
                "STARDAG_REGISTRY_URL": docker_services.api,
            }
            result = run_stardag_cli("config", "show", env=env)
            assert result.returncode == 0
            assert (
                docker_services.api in result.stdout
                or "localhost:8000" in result.stdout
            )

    def test_config_with_multiple_env_vars(
        self,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test config with multiple env vars set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HOME": tmpdir,
                "STARDAG_REGISTRY_URL": docker_services.api,
                "STARDAG_ORGANIZATION_ID": test_organization_id,
                "STARDAG_WORKSPACE_ID": test_workspace_id,
            }
            result = run_stardag_cli("config", "show", env=env)
            assert result.returncode == 0
            # Should show the configured values
            assert (
                test_organization_id in result.stdout
                or "Organization:" in result.stdout
            )


class TestCliRegistryCommands:
    """Test registry management commands."""

    def test_registry_list_empty(self) -> None:
        """Test registry list with no registries configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}
            result = run_stardag_cli("config", "registry", "list", env=env)
            assert result.returncode == 0

    def test_registry_add_and_list(
        self,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test adding and listing registries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}

            # Add a registry
            result = run_stardag_cli(
                "config",
                "registry",
                "add",
                "test-registry",
                "--url",
                docker_services.api,
                env=env,
            )
            assert result.returncode == 0

            # List registries
            result = run_stardag_cli("config", "registry", "list", env=env)
            assert result.returncode == 0
            assert "test-registry" in result.stdout

    def test_registry_remove(
        self,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test removing a registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}

            # Add a registry
            run_stardag_cli(
                "config",
                "registry",
                "add",
                "temp-registry",
                "--url",
                docker_services.api,
                env=env,
            )

            # Remove it
            result = run_stardag_cli(
                "config",
                "registry",
                "remove",
                "temp-registry",
                env=env,
            )
            assert result.returncode == 0

            # Verify it's gone
            result = run_stardag_cli("config", "registry", "list", env=env)
            assert "temp-registry" not in result.stdout


class TestCliProfileCommands:
    """Test profile management commands."""

    def test_profile_list_empty(self) -> None:
        """Test profile list with no profiles configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}
            result = run_stardag_cli("config", "profile", "list", env=env)
            assert result.returncode == 0

    def test_profile_add_requires_registry(self) -> None:
        """Test that profile add requires an existing registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}
            result = run_stardag_cli(
                "config",
                "profile",
                "add",
                "test-profile",
                "-r",
                "nonexistent",
                "-o",
                "test-org",
                "-w",
                "test-workspace",
                env=env,
                check=False,
            )
            # Should fail because registry doesn't exist
            assert result.returncode != 0
            assert (
                "not found" in result.stderr.lower() or "error" in result.stderr.lower()
            )


class TestCliLoginFlow:
    """Test login-related functionality (limited due to browser requirement)."""

    def test_login_with_api_key_set_shows_message(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test that login command shows message when API key is already set."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Login Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Try to login with API key already set
        env = {"STARDAG_API_KEY": api_key}
        result = run_stardag_cli("auth", "login", env=env)
        assert result.returncode == 0
        # Should indicate API key is already set
        assert (
            "API_KEY" in result.stdout
            or "already authenticated" in result.stdout.lower()
        )

    def test_logout_without_credentials(self) -> None:
        """Test logout when not logged in."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HOME": tmpdir, "STARDAG_PROFILE": ""}
            result = run_stardag_cli("auth", "logout", env=env)
            assert result.returncode == 0
            # Should indicate no credentials
            assert (
                "no credentials" in result.stdout.lower()
                or "cleared" in result.stdout.lower()
            )
