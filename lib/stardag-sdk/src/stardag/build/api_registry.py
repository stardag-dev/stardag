"""API-based registry that communicates with the stardag-api service."""

import getpass
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag._base import Task
from stardag.build.registry import RegistryABC, get_git_commit_hash

logger = logging.getLogger(__name__)


class APIRegistryConfig(BaseSettings):
    url: str | None = None
    timeout: float = 30.0

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_REGISTRY_")


class APIRegistry(RegistryABC):
    """Registry that stores task information via the stardag-api REST service."""

    def __init__(self, api_url: str, timeout: float = 30.0):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._client = None

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

    def register(self, task: Task) -> None:
        """Register a task with the API service."""
        task_data = {
            "task_id": task.task_id,
            "task_family": task.task_family,
            "task_data": task.model_dump(mode="json"),
            "user": getpass.getuser(),
            "commit_hash": get_git_commit_hash(),
            "dependency_ids": [dep.task_id for dep in task.deps()],
        }

        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/tasks",
                json=task_data,
            )
            if response.status_code == 409:
                # Task already exists, update it to completed
                self._mark_completed(task.task_id)
            elif response.status_code >= 400:
                logger.warning(
                    f"Failed to register task {task.task_id}: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to register task {task.task_id}: {e}")

    def start(self, task: Task) -> None:
        """Mark a task as started."""
        self._ensure_task_exists(task)
        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/tasks/{task.task_id}/start"
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to start task {task.task_id}: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to start task {task.task_id}: {e}")

    def complete(self, task: Task) -> None:
        """Mark a task as completed."""
        try:
            response = self.client.post(
                f"{self.api_url}/api/v1/tasks/{task.task_id}/complete"
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to complete task {task.task_id}: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to complete task {task.task_id}: {e}")

    def fail(self, task: Task, error_message: str | None = None) -> None:
        """Mark a task as failed."""
        try:
            params = {}
            if error_message:
                params["error_message"] = error_message
            response = self.client.post(
                f"{self.api_url}/api/v1/tasks/{task.task_id}/fail",
                params=params,
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Failed to mark task {task.task_id} as failed: "
                    f"{response.status_code} {response.text}"
                )
        except Exception as e:
            logger.warning(f"Failed to mark task {task.task_id} as failed: {e}")

    def _ensure_task_exists(self, task: Task) -> None:
        """Ensure a task exists in the registry, creating it if necessary."""
        response = self.client.get(f"{self.api_url}/api/v1/tasks/{task.task_id}")
        if response.status_code == 404:
            self.register(task)

    def _mark_completed(self, task_id: str) -> None:
        """Mark an existing task as completed."""
        try:
            self.client.post(f"{self.api_url}/api/v1/tasks/{task_id}/complete")
        except Exception as e:
            logger.warning(f"Failed to mark task {task_id} as completed: {e}")

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
