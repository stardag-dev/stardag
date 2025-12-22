"""API-based registry that communicates with the stardag-api service."""

import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag._base import Task
from stardag.build.registry import RegistryABC, get_git_commit_hash

logger = logging.getLogger(__name__)

TEST = "test"


class APIRegistryConfig(BaseSettings):
    """Configuration for APIRegistry via environment variables.

    Environment variables (prefixed with STARDAG_API_REGISTRY_):
    - STARDAG_API_REGISTRY_URL: API URL
    - STARDAG_API_REGISTRY_TIMEOUT: Request timeout
    - STARDAG_API_REGISTRY_WORKSPACE_ID: Workspace ID (required for JWT auth)

    Note: API key should be set via STARDAG_API_KEY (not prefixed).
    """

    url: str | None = None
    timeout: float = 30.0
    workspace_id: str | None = None  # Required for JWT auth, not needed for API key

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_REGISTRY_")


def _load_cli_settings() -> dict[str, str | float | None]:
    """Load credentials and config from CLI store if available.

    Returns dict with access_token, api_url, workspace_id, timeout (any may be None).
    Credentials (tokens) come from ~/.stardag/credentials.json
    Config (api_url, timeout, workspace_id) comes from ~/.stardag/config.json
    """
    result: dict[str, str | float | None] = {
        "access_token": None,
        "api_url": None,
        "workspace_id": None,
        "timeout": None,
    }

    try:
        from stardag.cli.credentials import load_config, load_credentials

        # Load OAuth credentials (tokens only)
        creds = load_credentials()
        if creds:
            result["access_token"] = creds.get("access_token")

        # Load config (api_url, timeout, workspace_id)
        config = load_config()
        if config:
            result["api_url"] = config.get("api_url")
            result["workspace_id"] = config.get("workspace_id")
            result["timeout"] = config.get("timeout")

    except ImportError:
        pass  # CLI not installed
    except Exception as e:
        logger.debug(f"Could not load CLI credentials/config: {e}")

    return result


class APIRegistry(RegistryABC):
    """Registry that stores task information via the stardag-api REST service.

    This registry implements build-scoped task tracking:
    1. Call start_build() at the beginning of a build to create a build record
    2. Tasks are registered within the build context
    3. Call complete_build() or fail_build() when the build finishes

    Authentication:
    - API key can be provided directly, via STARDAG_API_KEY env var,
      or loaded from CLI credentials (~/.stardag/credentials.json)
    - Priority: explicit api_key > STARDAG_API_KEY env var > CLI credentials
    """

    def __init__(
        self,
        api_url: str | None = None,
        timeout: float | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
    ):
        # Load CLI settings as fallback
        cli_settings = _load_cli_settings()

        # API key: explicit > env var only (not from CLI config)
        self.api_key = api_key or os.environ.get("STARDAG_API_KEY")

        # Access token from CLI browser login (for local dev, only if no API key)
        self.access_token = (
            cli_settings.get("access_token") if not self.api_key else None
        )

        # API URL priority: explicit > env var > CLI config > default
        cli_api_url = cli_settings.get("api_url")
        resolved_api_url = (
            api_url
            or os.environ.get("STARDAG_API_URL")
            or (cli_api_url if isinstance(cli_api_url, str) else None)
            or "http://localhost:8000"
        )
        self.api_url = resolved_api_url.rstrip("/")

        # Timeout priority: explicit > env var > CLI config > default
        env_timeout = os.environ.get("STARDAG_API_TIMEOUT")
        cli_timeout = cli_settings.get("timeout")
        self.timeout: float = (
            timeout
            if timeout is not None
            else (float(env_timeout) if env_timeout else None)
            or (cli_timeout if isinstance(cli_timeout, float) else None)
            or 30.0
        )

        # Workspace ID priority: explicit > env var > CLI config > None
        # (required for JWT auth, but API key auth gets workspace from key)
        cli_workspace_id = cli_settings.get("workspace_id")
        self.workspace_id: str | None = (
            workspace_id
            or os.environ.get("STARDAG_WORKSPACE_ID")
            or (cli_workspace_id if isinstance(cli_workspace_id, str) else None)
        )

        self._client = None
        self._build_id: str | None = None

        if self.api_key:
            logger.debug("APIRegistry initialized with API key authentication")
        elif self.access_token:
            if not self.workspace_id:
                logger.warning(
                    "APIRegistry: JWT auth requires workspace_id. "
                    "Run 'stardag config set workspace <id>' to set it."
                )
            else:
                logger.debug(
                    "APIRegistry initialized with browser login (JWT) authentication"
                )
        else:
            logger.warning(
                "APIRegistry initialized without authentication. "
                "Run 'stardag auth login' or set STARDAG_API_KEY env var."
            )

    @property
    def client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError:
                raise ImportError(
                    "httpx is required for APIRegistry. "
                    "Install it with: pip install stardag[api]"
                )
            # Create client with appropriate auth header
            headers = {}
            if self.api_key:
                # API key auth (production/CI)
                headers["X-API-Key"] = self.api_key
            elif self.access_token:
                # JWT auth from browser login (local dev)
                headers["Authorization"] = f"Bearer {self.access_token}"
            self._client = httpx.Client(timeout=self.timeout, headers=headers)
        return self._client

    @property
    def build_id(self) -> str | None:
        """Get the current build ID."""
        return self._build_id

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests.

        When using JWT auth, workspace_id must be passed as a query param.
        """
        if self.access_token and not self.api_key and self.workspace_id:
            return {"workspace_id": self.workspace_id}
        return {}

    def start_build(
        self,
        root_tasks: list[Task] | None = None,
        description: str | None = None,
    ) -> str:
        """Start a new build and return its ID.

        This should be called at the beginning of a build session.
        The workspace and user are determined from the API key authentication.
        """
        build_data = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [t.task_id for t in (root_tasks or [])],
            "description": description,
        }

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/builds",
                json=build_data,
                params=self._get_params(),
            )
            response.raise_for_status()
            data = response.json()
            build_id: str = data["id"]
            self._build_id = build_id
            logger.info(f"Started build: {data['name']} (ID: {build_id})")
            return build_id
        except Exception as e:
            logger.warning(f"Failed to start build: {e}")
            raise

    def complete_build(self) -> None:
        """Mark the current build as completed."""
        if self._build_id is None:
            logger.warning("No active build to complete")
            return

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/builds/{self._build_id}/complete",
                params=self._get_params(),
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to complete build {self._build_id}: "
                    f"{response.status_code} {response.text}"
                )
            else:
                logger.info(f"Completed build: {self._build_id}")
        except Exception as e:
            logger.warning(f"Failed to complete build {self._build_id}: {e}")
            raise

    def fail_build(self, error_message: str | None = None) -> None:
        """Mark the current build as failed."""
        if self._build_id is None:
            logger.warning("No active build to fail")
            return

        try:
            params = self._get_params()
            if error_message:
                params["error_message"] = error_message
            response = self.client.post(
                f"{self.api_url}/api/v1/builds/{self._build_id}/fail",
                params=params,
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to mark build {self._build_id} as failed: "
                    f"{response.status_code} {response.text}"
                )
            else:
                logger.info(f"Marked build as failed: {self._build_id}")
        except Exception as e:
            logger.warning(f"Failed to mark build {self._build_id} as failed: {e}")
            raise

    def register(self, task: Task) -> None:
        """Register a task with the API service within the current build."""
        if self._build_id is None:
            # Auto-start a build if none exists
            self.start_build(root_tasks=[task])

        task_data = {
            "task_id": task.task_id,
            "task_namespace": task.get_namespace(),
            "task_family": task.get_family(),
            "task_data": task.model_dump(mode="json"),
            "version": None,
            "dependency_task_ids": [dep.task_id for dep in task.deps()],
        }

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/builds/{self._build_id}/tasks",
                json=task_data,
                params=self._get_params(),
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to register task {task.task_id}: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to register task {task.task_id}: {e}")
            raise

    def start(self, task: Task) -> None:
        """Mark a task as started within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot start task")
            return

        # Ensure task is registered first
        self.register(task)

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.task_id}/start",
                params=self._get_params(),
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to start task {task.task_id}: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to start task {task.task_id}: {e}")
            raise

    def complete(self, task: Task) -> None:
        """Mark a task as completed within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot complete task")
            return

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.task_id}/complete",
                params=self._get_params(),
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to complete task {task.task_id}: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to complete task {task.task_id}: {e}")
            raise

    def fail(self, task: Task, error_message: str | None = None) -> None:
        """Mark a task as failed within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot fail task")
            return

        try:
            params = self._get_params()
            if error_message:
                params["error_message"] = error_message
            response = self.client.post(
                f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.task_id}/fail",
                params=params,
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to mark task {task.task_id} as failed: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to mark task {task.task_id} as failed: {e}")
            raise

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
