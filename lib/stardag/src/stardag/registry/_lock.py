"""Registry-based global concurrency lock implementation."""

import logging

from stardag.build._base import (
    GlobalConcurrencyLock,
    LockAcquisitionResult,
    LockAcquisitionStatus,
)
from stardag.config import config_provider
from stardag.exceptions import APIError

logger = logging.getLogger(__name__)


class RegistryGlobalConcurrencyLock:
    """Global concurrency lock backed by the Stardag Registry API.

    Implements the GlobalConcurrencyLock protocol using HTTP calls to the
    stardag-api lock endpoints.

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

        self._async_client = None

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests.

        When using JWT auth, workspace_id must be passed as a query param.
        """
        if self.access_token and not self.api_key and self.workspace_id:
            return {"workspace_id": self.workspace_id}
        return {}

    def _handle_response_error(
        self, response, operation: str = "Lock operation"
    ) -> None:
        """Check response for errors and raise appropriate exceptions."""
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

        # For lock operations, we handle specific status codes differently
        if status_code == 423:
            # Lock held by another owner - don't raise, return result
            return
        if status_code == 429:
            # Concurrency limit reached - don't raise, return result
            return
        if status_code == 409:
            # Conflict (not owner) - don't raise, return result
            return

        # For other errors, raise
        raise APIError(f"{operation} failed", status_code=status_code, detail=detail)

    @property
    def async_client(self):
        """Lazy-initialized async HTTP client."""
        if self._async_client is None:
            try:
                import httpx
            except ImportError:
                raise ImportError(
                    "httpx is required for RegistryGlobalConcurrencyLock. "
                    "Install it with: pip install stardag[api]"
                )
            headers = {}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            elif self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            self._async_client = httpx.AsyncClient(
                timeout=self.timeout, headers=headers
            )
        return self._async_client

    async def acquire(
        self,
        task_id: str,
        owner_id: str,
        ttl_seconds: int = 60,
        check_task_completion: bool = True,
    ) -> LockAcquisitionResult:
        """Acquire a lock for a task.

        Args:
            task_id: The task identifier (hash).
            owner_id: UUID identifying the lock owner (stable across retries).
            ttl_seconds: Time-to-live in seconds for the lock.
            check_task_completion: If True, check if task is already completed
                in the registry before acquiring.

        Returns:
            LockAcquisitionResult with status.
        """
        try:
            response = await self.async_client.post(
                f"{self.api_url}/api/v1/locks/{task_id}/acquire",
                json={
                    "owner_id": owner_id,
                    "ttl_seconds": ttl_seconds,
                    "check_task_completion": check_task_completion,
                },
                params=self._get_params(),
            )

            # Handle error responses
            if response.status_code == 423:
                # Lock held by another owner
                data = response.json()
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.HELD_BY_OTHER,
                    acquired=False,
                    error_message=data.get("detail", {}).get("error_message"),
                )

            if response.status_code == 429:
                # Concurrency limit reached
                data = response.json()
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.CONCURRENCY_LIMIT_REACHED,
                    acquired=False,
                    error_message=data.get("detail", {}).get("error_message"),
                )

            self._handle_response_error(response, f"Acquire lock for {task_id}")

            data = response.json()
            status = LockAcquisitionStatus(data["status"])

            return LockAcquisitionResult(
                status=status,
                acquired=data["acquired"],
                error_message=data.get("error_message"),
            )

        except Exception as e:
            logger.error(f"Failed to acquire lock for {task_id}: {e}")
            return LockAcquisitionResult(
                status=LockAcquisitionStatus.ERROR,
                acquired=False,
                error_message=str(e),
            )

    async def renew(
        self,
        task_id: str,
        owner_id: str,
        ttl_seconds: int = 60,
    ) -> bool:
        """Renew a lock's TTL.

        Args:
            task_id: The task identifier.
            owner_id: The expected owner.
            ttl_seconds: New TTL in seconds.

        Returns:
            True if successfully renewed, False otherwise.
        """
        try:
            response = await self.async_client.post(
                f"{self.api_url}/api/v1/locks/{task_id}/renew",
                json={
                    "owner_id": owner_id,
                    "ttl_seconds": ttl_seconds,
                },
                params=self._get_params(),
            )

            if response.status_code == 409:
                # Not owner or lock doesn't exist
                return False

            self._handle_response_error(response, f"Renew lock for {task_id}")

            data = response.json()
            return data.get("renewed", False)

        except Exception as e:
            logger.error(f"Failed to renew lock for {task_id}: {e}")
            return False

    async def release(
        self,
        task_id: str,
        owner_id: str,
        task_completed: bool = False,
        build_id: str | None = None,
    ) -> bool:
        """Release a lock.

        Args:
            task_id: The task identifier.
            owner_id: The expected owner.
            task_completed: If True and build_id provided, record task completion
                in the same transaction.
            build_id: Build ID for completion recording.

        Returns:
            True if successfully released, False otherwise.
        """
        try:
            response = await self.async_client.post(
                f"{self.api_url}/api/v1/locks/{task_id}/release",
                json={
                    "owner_id": owner_id,
                    "task_completed": task_completed,
                    "build_id": build_id,
                },
                params=self._get_params(),
            )

            if response.status_code == 409:
                # Not owner or lock doesn't exist
                logger.warning(f"Failed to release lock for {task_id}: not owner")
                return False

            self._handle_response_error(response, f"Release lock for {task_id}")

            data = response.json()
            return data.get("released", False)

        except Exception as e:
            logger.error(f"Failed to release lock for {task_id}: {e}")
            return False

    async def check_task_completed(
        self,
        task_id: str,
    ) -> bool:
        """Check if a task is registered as completed in the registry.

        Args:
            task_id: The task identifier.

        Returns:
            True if task has a completion record.
        """
        try:
            response = await self.async_client.get(
                f"{self.api_url}/api/v1/locks/tasks/{task_id}/completion-status",
                params=self._get_params(),
            )

            self._handle_response_error(
                response, f"Check completion status for {task_id}"
            )

            data = response.json()
            return data.get("is_completed", False)

        except Exception as e:
            logger.error(f"Failed to check completion status for {task_id}: {e}")
            return False

    async def aclose(self) -> None:
        """Close the async HTTP client."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()
        return False


# Type assertion to ensure RegistryGlobalConcurrencyLock implements the protocol
_: GlobalConcurrencyLock = RegistryGlobalConcurrencyLock()  # type: ignore[assignment]
