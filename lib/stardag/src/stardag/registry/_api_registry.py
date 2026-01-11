"""API-based registry that communicates with the stardag-api service."""

import logging

from stardag._registry_asset import RegistryAsset
from stardag._task import BaseTask, flatten_task_struct
from stardag.registry._base import RegistryABC, get_git_commit_hash
from stardag.registry._http_client import (
    RegistryAPIAsyncHTTPClient,
    RegistryAPIClientConfig,
    RegistryAPISyncHTTPClient,
    get_async_http_client,
    get_sync_http_client,
    handle_response_error,
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
        # HTTP client configuration (shared with RegistryGlobalConcurrencyLockManager)
        self._client_config = RegistryAPIClientConfig.from_config(
            api_url=api_url,
            timeout=timeout,
            workspace_id=workspace_id,
            api_key=api_key,
        )

        self._client: RegistryAPISyncHTTPClient | None = None
        self._async_client: RegistryAPIAsyncHTTPClient | None = None
        self._build_id: str | None = None

        # Log authentication status
        self._client_config.log_auth_status("APIRegistry")

    @property
    def api_url(self) -> str:
        """Base API URL."""
        return self._client_config.api_url

    @property
    def workspace_id(self) -> str | None:
        """Workspace ID for JWT auth."""
        return self._client_config.workspace_id

    def _handle_response_error(self, response, operation: str = "API call") -> None:
        """Check response for errors and raise appropriate exceptions."""
        handle_response_error(
            response, operation=operation, workspace_id=self.workspace_id
        )

    @property
    def client(self) -> RegistryAPISyncHTTPClient:
        """Lazy-initialized sync HTTP client."""
        if self._client is None:
            self._client = get_sync_http_client(self._client_config)
        return self._client

    @property
    def build_id(self) -> str | None:
        """Get the current build ID."""
        return self._build_id

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests."""
        return self._client_config.get_workspace_params()

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
            "root_task_ids": [str(task.id) for task in (root_tasks or [])],
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

    def register_task(self, task: BaseTask) -> None:
        """Register a task with the API service within the current build."""
        if self._build_id is None:
            # Auto-start a build if none exists
            self.start_build(root_tasks=[task])

        task_data = {
            # TODO: rename keys (drop "task_" prefix)
            "task_id": str(task.id),
            "task_namespace": task.get_namespace(),
            "task_name": task.get_name(),
            "task_data": task.model_dump(mode="json"),
            "version": task.version,
            "dependency_task_ids": [
                str(dep.id) for dep in flatten_task_struct(task.requires())
            ],
        }

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks",
            json=task_data,
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Register task {task.id}")

    def start_task(self, task: BaseTask) -> None:
        """Mark a task as started within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot start task")
            return

        # Ensure task is registered first
        self.register_task(task)

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/start",
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Start task {task.id}")

    def complete_task(self, task: BaseTask) -> None:
        """Mark a task as completed within the current build."""
        if self._build_id is None:
            logger.warning("No active build - cannot complete task")
            return

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/complete",
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Complete task {task.id}")

    def fail_task(self, task: BaseTask, error_message: str | None = None) -> None:
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

    def upload_task_assets(self, task: BaseTask, assets: list[RegistryAsset]) -> None:
        """Upload assets for a completed task."""
        if self._build_id is None:
            logger.warning("No active build - cannot upload task assets")
            return

        if not assets:
            return

        # Serialize assets to API format
        # For all asset types, body is stored as a dict in body_json
        # - markdown: {"content": "<markdown string>"}
        # - json: the actual JSON data dict
        assets_data = []
        for asset in assets:
            data = asset.model_dump(mode="json")
            if asset.type == "markdown":
                # Wrap markdown body string in {"content": ...} dict
                data["body"] = {"content": data["body"]}
            assets_data.append(data)

        response = self.client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/assets",
            json=assets_data,
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Upload assets for task {task.id}")
        logger.debug(f"Uploaded {len(assets)} assets for task {task.id}")

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
        """Close the async HTTP client."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()
        return False

    # Async client and methods

    @property
    def async_client(self) -> RegistryAPIAsyncHTTPClient:
        """Lazy-initialized async HTTP client."""
        if self._async_client is None:
            self._async_client = get_async_http_client(self._client_config)
        return self._async_client

    async def start_build_aio(
        self,
        root_tasks: list[BaseTask] | None = None,
        description: str | None = None,
    ) -> str:
        """Async version - start a new build and return its ID."""
        build_data = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [str(task.id) for task in (root_tasks or [])],
            "description": description,
        }

        response = await self.async_client.post(
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

    async def complete_build_aio(self) -> None:
        """Async version - mark the current build as completed."""
        if self._build_id is None:
            logger.warning("No active build to complete")
            return

        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/complete",
            params=self._get_params(),
        )
        self._handle_response_error(response, "Complete build")
        logger.info(f"Completed build: {self._build_id}")

    async def fail_build_aio(self, error_message: str | None = None) -> None:
        """Async version - mark the current build as failed."""
        if self._build_id is None:
            logger.warning("No active build to fail")
            return

        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/fail",
            params=params,
        )
        self._handle_response_error(response, "Fail build")
        logger.info(f"Marked build as failed: {self._build_id}")

    async def register_task_aio(self, task: BaseTask) -> None:
        """Async version - register a task within the current build."""
        if self._build_id is None:
            await self.start_build_aio(root_tasks=[task])

        task_data = {
            "task_id": str(task.id),
            "task_namespace": task.get_namespace(),
            "task_name": task.get_name(),
            "task_data": task.model_dump(mode="json"),
            "version": task.version,
            "dependency_task_ids": [
                str(dep.id) for dep in flatten_task_struct(task.requires())
            ],
        }

        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks",
            json=task_data,
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Register task {task.id}")

    async def start_task_aio(self, task: BaseTask) -> None:
        """Async version - mark a task as started."""
        if self._build_id is None:
            logger.warning("No active build - cannot start task")
            return

        await self.register_task_aio(task)

        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/start",
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Start task {task.id}")

    async def complete_task_aio(self, task: BaseTask) -> None:
        """Async version - mark a task as completed."""
        if self._build_id is None:
            logger.warning("No active build - cannot complete task")
            return

        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/complete",
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Complete task {task.id}")

    async def fail_task_aio(
        self, task: BaseTask, error_message: str | None = None
    ) -> None:
        """Async version - mark a task as failed."""
        if self._build_id is None:
            logger.warning("No active build - cannot fail task")
            return

        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/fail",
            params=params,
        )
        self._handle_response_error(response, f"Fail task {task.id}")

    async def upload_task_assets_aio(
        self, task: BaseTask, assets: list[RegistryAsset]
    ) -> None:
        """Async version - upload assets for a completed task."""
        if self._build_id is None:
            logger.warning("No active build - cannot upload task assets")
            return

        if not assets:
            return

        assets_data = []
        for asset in assets:
            data = asset.model_dump(mode="json")
            if asset.type == "markdown":
                data["body"] = {"content": data["body"]}
            assets_data.append(data)

        response = await self.async_client.post(
            f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.id}/assets",
            json=assets_data,
            params=self._get_params(),
        )
        self._handle_response_error(response, f"Upload assets for task {task.id}")
        logger.debug(f"Uploaded {len(assets)} assets for task {task.id}")
