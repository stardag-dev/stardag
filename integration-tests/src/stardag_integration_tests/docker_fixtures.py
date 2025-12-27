"""Docker Compose fixtures for integration tests.

This module provides fixtures and utilities for managing docker-compose services
during integration tests.
"""

import logging
import os
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

logger = logging.getLogger(__name__)

# Service URLs (matching docker-compose.yml)
API_URL = os.getenv("STARDAG_API_URL", "http://localhost:8000")
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
UI_URL = os.getenv("STARDAG_UI_URL", "http://localhost:3000")
DB_HOST = os.getenv("STARDAG_DB_HOST", "localhost")
DB_PORT = int(os.getenv("STARDAG_DB_PORT", "5432"))

# Keycloak test user credentials (from realm-export.json)
TEST_USER_EMAIL = "testuser@localhost"
TEST_USER_PASSWORD = "testpassword"
TEST_USER_USERNAME = "testuser"

# Timeouts
SERVICE_STARTUP_TIMEOUT = 120  # seconds
HEALTH_CHECK_INTERVAL = 2  # seconds


@dataclass
class ServiceEndpoints:
    """Container for service endpoint URLs."""

    api: str = API_URL
    keycloak: str = KEYCLOAK_URL
    ui: str = UI_URL

    @property
    def oidc_issuer(self) -> str:
        """OIDC issuer URL."""
        return f"{self.keycloak}/realms/stardag"

    @property
    def oidc_issuer_external(self) -> str:
        """External OIDC issuer URL (for browser-based flows)."""
        return "http://localhost:8080/realms/stardag"


def get_repo_root() -> Path:
    """Get the repository root directory."""
    # Navigate from tests/integration/ to repo root
    current = Path(__file__).parent
    while current != current.parent:
        if (current / "docker-compose.yml").exists():
            return current
        current = current.parent
    raise RuntimeError("Could not find repository root (no docker-compose.yml found)")


def run_docker_compose(
    *args: str,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a docker-compose command.

    Args:
        *args: Arguments to pass to docker-compose
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit code

    Returns:
        CompletedProcess instance
    """
    repo_root = get_repo_root()
    cmd = ["docker-compose", *args]
    logger.info("Running: %s (in %s)", " ".join(cmd), repo_root)

    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=capture_output,
        text=True,
        check=check,
    )

    if result.returncode != 0 and capture_output:
        logger.error("Command failed with stderr: %s", result.stderr)

    return result


def wait_for_health(
    url: str,
    timeout: float = SERVICE_STARTUP_TIMEOUT,
    interval: float = HEALTH_CHECK_INTERVAL,
) -> bool:
    """Wait for a health endpoint to return 200.

    Args:
        url: URL to check
        timeout: Maximum time to wait
        interval: Time between checks

    Returns:
        True if healthy, False if timeout reached
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200:
                logger.info("Service healthy: %s", url)
                return True
        except httpx.RequestError as e:
            logger.debug("Health check failed for %s: %s", url, e)
        time.sleep(interval)

    logger.error("Service did not become healthy: %s", url)
    return False


def wait_for_keycloak(
    base_url: str = KEYCLOAK_URL,
    timeout: float = SERVICE_STARTUP_TIMEOUT,
) -> bool:
    """Wait for Keycloak to be ready.

    Keycloak takes longer to start due to database initialization.
    We check the OIDC well-known endpoint since the health endpoint
    is on a separate management port that may not be exposed.
    """
    # Check the stardag realm's OIDC configuration endpoint
    oidc_config_url = f"{base_url}/realms/stardag/.well-known/openid-configuration"
    logger.info("Waiting for Keycloak at %s...", oidc_config_url)

    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(oidc_config_url, timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                if "token_endpoint" in data:
                    logger.info("Keycloak is ready")
                    return True
        except (httpx.RequestError, ValueError) as e:
            logger.debug("Keycloak health check failed: %s", e)
        time.sleep(HEALTH_CHECK_INTERVAL)

    logger.error("Keycloak did not become ready within %s seconds", timeout)
    return False


def wait_for_api(
    base_url: str = API_URL,
    timeout: float = SERVICE_STARTUP_TIMEOUT,
) -> bool:
    """Wait for the API to be ready."""
    health_url = f"{base_url}/health"
    logger.info("Waiting for API at %s...", health_url)
    return wait_for_health(health_url, timeout)


def wait_for_ui(
    base_url: str = UI_URL,
    timeout: float = SERVICE_STARTUP_TIMEOUT,
) -> bool:
    """Wait for the UI to be ready."""
    logger.info("Waiting for UI at %s...", base_url)
    return wait_for_health(base_url, timeout)


def docker_compose_up(
    services: list[str] | None = None,
    build: bool = False,
    wait: bool = True,
) -> None:
    """Start docker-compose services.

    Args:
        services: Specific services to start (None = all)
        build: Whether to rebuild images
        wait: Whether to wait for services to be healthy
    """
    args = ["up", "-d"]
    if build:
        args.append("--build")
    if services:
        args.extend(services)

    run_docker_compose(*args)

    if wait:
        # Wait for services in order (dependencies first)
        if not services or "keycloak" in services:
            if not wait_for_keycloak():
                raise RuntimeError("Keycloak failed to start")

        if not services or "api" in services:
            if not wait_for_api():
                raise RuntimeError("API failed to start")

        if not services or "ui" in services:
            if not wait_for_ui():
                raise RuntimeError("UI failed to start")


def docker_compose_down(volumes: bool = False) -> None:
    """Stop docker-compose services.

    Args:
        volumes: Whether to remove volumes (clean slate)
    """
    args = ["down"]
    if volumes:
        args.append("-v")
    run_docker_compose(*args, check=False)


def docker_compose_logs(services: list[str] | None = None) -> str:
    """Get logs from docker-compose services.

    Args:
        services: Specific services to get logs from (None = all)

    Returns:
        Combined logs as string
    """
    args = ["logs", "--tail=100"]
    if services:
        args.extend(services)

    result = run_docker_compose(*args, check=False)
    return result.stdout


def is_docker_compose_running() -> bool:
    """Check if docker-compose services are running."""
    result = run_docker_compose("ps", "--quiet", check=False)
    return bool(result.stdout.strip())


# --- Pytest Fixtures ---


@pytest.fixture(scope="session")
def docker_services() -> Generator[ServiceEndpoints, None, None]:
    """Session-scoped fixture that ensures docker-compose services are running.

    This fixture:
    - Checks if services are already running
    - If not, starts them (with build)
    - Waits for all services to be healthy
    - Yields the service endpoints
    - Does NOT tear down (to allow reuse across test runs)

    To force a clean start, run `docker-compose down -v` before tests.
    """
    endpoints = ServiceEndpoints()

    # Check if services are already healthy
    api_healthy = False
    keycloak_healthy = False

    try:
        api_healthy = wait_for_api(timeout=5)
    except Exception:
        pass

    try:
        keycloak_healthy = wait_for_keycloak(timeout=5)
    except Exception:
        pass

    if api_healthy and keycloak_healthy:
        logger.info("Docker services already running")
    else:
        logger.info("Starting docker-compose services...")
        docker_compose_up(build=True, wait=True)

    yield endpoints

    # Don't tear down - leave services running for faster re-runs
    logger.info("Leaving docker services running for potential reuse")


@pytest.fixture(scope="session")
def api_url(docker_services: ServiceEndpoints) -> str:
    """Get the API URL."""
    return docker_services.api


@pytest.fixture(scope="session")
def keycloak_url(docker_services: ServiceEndpoints) -> str:
    """Get the Keycloak URL."""
    return docker_services.keycloak


@pytest.fixture(scope="session")
def ui_url(docker_services: ServiceEndpoints) -> str:
    """Get the UI URL."""
    return docker_services.ui


@pytest.fixture(scope="session")
def oidc_issuer(docker_services: ServiceEndpoints) -> str:
    """Get the OIDC issuer URL."""
    return docker_services.oidc_issuer


@pytest.fixture
def docker_logs_on_failure(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Fixture that prints docker logs if the test fails."""
    yield

    # Check if test failed
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        logger.error("Test failed, printing docker logs...")
        logs = docker_compose_logs()
        print("\n" + "=" * 80)
        print("DOCKER COMPOSE LOGS (last 100 lines per service)")
        print("=" * 80)
        print(logs)
        print("=" * 80 + "\n")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Generator:  # noqa: ARG001
    """Hook to store test result on the item for docker_logs_on_failure fixture."""
    outcome = yield
    if outcome is not None:
        rep = outcome.get_result()
        setattr(item, f"rep_{rep.when}", rep)
