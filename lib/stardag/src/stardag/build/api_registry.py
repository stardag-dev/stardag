"""API-based registry that communicates with the stardag-api service."""

import logging

from stardag._task import BaseTask, flatten_task_struct
from stardag.build.registry import RegistryABC, get_git_commit_hash
from stardag.config import config_provider
from stardag.exceptions import (
    APIError,
    AuthorizationError,
    InvalidAPIKeyError,
    InvalidTokenError,
    NotAuthenticatedError,
    NotFoundError,
    TokenExpiredError,
    WorkspaceAccessError,
)

logger = logging.getLogger(__name__)


class APIRegistry(RegistryABC):
    """Registry that stores task information via the stardag-api REST service.

    This registry implements build-scoped task tracking:
    1. Call start_build() at the beginning of a build to create a build record
    2. Tasks are registered within the build context
    3. Call complete_build() or fail_build() when the build finishes

    Authentication:
    - API key can be provided directly or via STARDAG_API_KEY env var
    - JWT token from browser login (stored in registry credentials)

    Configuration is loaded from the central config module (stardag.config).
    """

    def __init__(
        self,
        api_url: str | None = None,
        timeout: float | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
    ):
        # Load central config
        config = config_provider.get()

        # API key: explicit > config (which includes env var)
        self.api_key = api_key or config.api_key

        # Access token from config (browser login, only if no API key)
        self.access_token = config.access_token if not self.api_key else None

        # API URL: explicit > config
        self.api_url = (api_url or config.api.url).rstrip("/")

        # Timeout: explicit > config
        self.timeout = timeout if timeout is not None else config.api.timeout

        # Workspace ID: explicit > config
        self.workspace_id = workspace_id or config.context.workspace_id

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

    def _handle_response_error(self, response, operation: str = "API call") -> None:
        """Check response for errors and raise appropriate exceptions.

        Args:
            response: httpx Response object
            operation: Description of the operation for error messages

        Raises:
            TokenExpiredError: If token has expired
            InvalidTokenError: If token is invalid
            InvalidAPIKeyError: If API key is invalid
            NotAuthenticatedError: If no auth provided
            WorkspaceAccessError: If workspace access denied
            AuthorizationError: If other 403 error
            NotFoundError: If resource not found
            APIError: For other HTTP errors
        """
        if response.status_code < 400:
            return  # No error

        # Try to extract detail from response JSON
        detail = None
        try:
            data = response.json()
            detail = data.get("detail", str(data))
        except Exception:
            detail = response.text[:200] if response.text else None

        status_code = response.status_code

        if status_code == 401:
            # Authentication error - determine specific type
            detail_lower = (detail or "").lower()
            if "expired" in detail_lower:
                raise TokenExpiredError(detail)
            elif "api key" in detail_lower:
                raise InvalidAPIKeyError(detail)
            elif "not authenticated" in detail_lower or not detail:
                raise NotAuthenticatedError(detail)
            else:
                raise InvalidTokenError(detail)

        elif status_code == 403:
            # Authorization error
            detail_lower = (detail or "").lower()
            if "workspace" in detail_lower:
                raise WorkspaceAccessError(
                    workspace_id=self.workspace_id, detail=detail
                )
            else:
                raise AuthorizationError(f"{operation} access denied", detail=detail)

        elif status_code == 404:
            raise NotFoundError(f"{operation}: resource not found", detail=detail)

        else:
            raise APIError(
                f"{operation} failed", status_code=status_code, detail=detail
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
        root_tasks: list[BaseTask] | None = None,
        description: str | None = None,
    ) -> str:
        """Start a new build and return its ID.

        This should be called at the beginning of a build session.
        The workspace and user are determined from the API key authentication.
        """
        build_data = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [task.id for task in (root_tasks or [])],
            "description": description,
        }

        response = self.client.post(
            f"{self.api_url}/api/v1/builds",
            json=build_data,
            params=self._get_params(),
        )
        self._handle_response_error(response, "Start build")
        data = response.json()
        build_id: str = data["id"]
        self._build_id = build_id
        logger.info(f"Started build: {data['name']} (ID: {build_id})")
        return build_id

    def complete_build(self) -> None:
        """Mark the current build as completed."""
        if self._build_id is None:
            logger.warning("No active build to complete")
            return

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/complete",
            params=self._get_params(),
        )
        self._handle_response_error(response, "Complete build")
        logger.info(f"Completed build: {self._build_id}")

    def fail_build(self, error_message: str | None = None) -> None:
        """Mark the current build as failed."""
        if self._build_id is None:
            logger.warning("No active build to fail")
            return

        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/fail",
            params=params,
        )
        self._handle_response_error(response, "Fail build")
        logger.info(f"Marked build as failed: {self._build_id}")

    def register(self, task: BaseTask) -> None:
        """Register a task with the API service within the current build."""
        if self._build_id is None:
            # Auto-start a build if none exists
            self.start_build(root_tasks=[task])

        task_data = {
            # TODO: rename keys (drop "task_" prefix)
            "task_id": task.id,
            "task_namespace": task.get_type_namespace(),
            "task_family": task.get_type_name(),
            "task_data": task.model_dump(mode="json"),
            "version": task.version,
            "dependency_task_ids": [
                dep.id for dep in flatten_task_struct(task.requires())
            ],
        }

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks",
            json=task_data,
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Register task {task.id}")

    def start(self, task: BaseTask) -> None:
        """Mark a task as started within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot start task")
            return

        # Ensure task is registered first
        self.register(task)

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/start",
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Start task {task.id}")

    def complete(self, task: BaseTask) -> None:
        """Mark a task as completed within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot complete task")
            return

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/complete",
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Complete task {task.id}")

    def fail(self, task: BaseTask, error_message: str | None = None) -> None:
        """Mark a task as failed within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot fail task")
            return

        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/fail",
            params=params,
        )
        self._handle_response_error(response, f"Fail task {task.id}")

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
