"""API-based registry that communicates with the stardag-api service."""

import getpass
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag._base import Task
from stardag.build.registry import RegistryABC, get_git_commit_hash

logger = logging.getLogger(__name__)

TEST = "test"


class APIRegistryConfig(BaseSettings):
    url: str | None = None
    timeout: float = 30.0
    workspace_id: str = "default"

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_REGISTRY_")


class APIRegistry(RegistryABC):
    """Registry that stores task information via the stardag-api REST service.

    This registry implements build-scoped task tracking:
    1. Call start_build() at the beginning of a build to create a build record
    2. Tasks are registered within the build context
    3. Call complete_build() or fail_build() when the build finishes
    """

    def __init__(
        self,
        api_url: str,
        timeout: float = 30.0,
        workspace_id: str = "default",
    ):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.workspace_id = workspace_id
        self._client = None
        self._build_id: str | None = None

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
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    @property
    def build_id(self) -> str | None:
        """Get the current build ID."""
        return self._build_id

    def start_build(
        self,
        root_tasks: list[Task] | None = None,
        description: str | None = None,
    ) -> str:
        """Start a new build and return its ID.

        This should be called at the beginning of a build session.
        """
        build_data = {
            "workspace_id": self.workspace_id,
            "user": getpass.getuser(),
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [t.task_id for t in (root_tasks or [])],
            "description": description,
        }

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/builds",
                json=build_data,
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
                f"{self.api_url}/api/v1/builds/{self._build_id}/complete"
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
            params = {}
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
                f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.task_id}/start"
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
                f"{self.api_url}/api/v1/builds/{self._build_id}/tasks/{task.task_id}/complete"
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
            params = {}
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
