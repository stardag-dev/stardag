"""Registry-based global concurrency lock implementation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stardag.build._base import (
    GlobalConcurrencyLockManager,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    LockHandle,
)
from stardag.registry._http_client import (
    RegistryAPIAsyncHTTPClient,
    RegistryAPIClientConfig,
    get_async_http_client,
    handle_response_error,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class RegistryLockManagerConfig:
    """Configuration for RegistryGlobalConcurrencyLockManager.

    Attributes:
        ttl_seconds: Lock time-to-live in seconds. The lock will auto-expire
            after this time if not renewed or released.
        renewal_interval_seconds: How often to renew locks during execution.
            Should be less than ttl_seconds. Set to None to disable auto-renewal.
        check_task_completion: Whether to check if task is already completed
            in the registry before acquiring the lock.
        build_id: Optional build ID for recording task completion on release.
    """

    ttl_seconds: int = 60
    renewal_interval_seconds: float | None = 20.0
    check_task_completion: bool = True
    build_id: str | None = None


class RegistryLockHandle:
    """Lock handle for Registry-based locks with automatic renewal.

    Implements the LockHandle protocol. Manages automatic TTL renewal
    via a background task while the lock is held.
    """

    def __init__(
        self,
        manager: RegistryGlobalConcurrencyLockManager,
        task_id: str,
        config: RegistryLockManagerConfig,
    ):
        self._manager = manager
        self._task_id = task_id
        self._config = config
        self._result: LockAcquisitionResult | None = None
        self._task_completed = False
        self._renewal_task: asyncio.Task | None = None
        self._stop_renewal = asyncio.Event()

    @property
    def result(self) -> LockAcquisitionResult:
        """The result of the lock acquisition attempt."""
        if self._result is None:
            raise RuntimeError("Lock not yet acquired - use 'async with' context")
        return self._result

    def mark_completed(self) -> None:
        """Mark that the task completed successfully."""
        self._task_completed = True

    async def __aenter__(self) -> RegistryLockHandle:
        """Acquire the lock and start renewal if needed."""
        self._result = await self._manager._acquire_internal(
            self._task_id,
            self._config.ttl_seconds,
            self._config.check_task_completion,
        )

        # Start renewal task if lock was acquired and renewal is configured
        if self._result.acquired and self._config.renewal_interval_seconds is not None:
            self._renewal_task = asyncio.create_task(self._renewal_loop())

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Stop renewal and release the lock."""
        # Stop renewal task
        if self._renewal_task is not None:
            self._stop_renewal.set()
            try:
                await asyncio.wait_for(self._renewal_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._renewal_task.cancel()
                try:
                    await self._renewal_task
                except asyncio.CancelledError:
                    pass

        # Release lock if it was acquired
        if self._result is not None and self._result.acquired:
            await self._manager._release_internal(
                self._task_id,
                self._task_completed,
                self._config.build_id,
            )

        return False  # Don't suppress exceptions

    async def _renewal_loop(self) -> None:
        """Background task to periodically renew the lock."""
        interval = self._config.renewal_interval_seconds
        if interval is None:
            return

        while not self._stop_renewal.is_set():
            try:
                # Wait for interval or stop signal
                try:
                    await asyncio.wait_for(
                        self._stop_renewal.wait(),
                        timeout=interval,
                    )
                    # Stop signal received
                    break
                except asyncio.TimeoutError:
                    # Time to renew
                    pass

                # Renew the lock
                success = await self._manager._renew_internal(
                    self._task_id,
                    self._config.ttl_seconds,
                )
                if not success:
                    logger.warning(
                        f"Failed to renew lock for task {self._task_id}. "
                        "Lock may have expired."
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in lock renewal loop for {self._task_id}: {e}")


class RegistryGlobalConcurrencyLockManager:
    """Global concurrency lock manager backed by the Stardag Registry API.

    Implements the GlobalConcurrencyLockManager protocol using HTTP calls to the
    stardag-api lock endpoints. Handles automatic TTL renewal internally.

    Usage:
        lock_manager = RegistryGlobalConcurrencyLockManager(
            owner_id="build-uuid-123",
            config=RegistryLockManagerConfig(ttl_seconds=60),
        )

        async with lock_manager.lock("task-id") as handle:
            if handle.result.acquired:
                # execute task
                handle.mark_completed()

    Authentication:
    - API key can be provided directly or via STARDAG_API_KEY env var
    - JWT token from browser login (stored in registry credentials)

    Configuration is loaded from the central config module (stardag.config).
    """

    def __init__(
        self,
        owner_id: str | None = None,
        config: RegistryLockManagerConfig | None = None,
        api_url: str | None = None,
        timeout: float | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
    ):
        """Initialize the lock manager.

        Args:
            owner_id: Unique identifier for this lock owner. If not provided,
                a random UUID will be generated. Should be stable across retries
                within the same build process.
            config: Lock configuration (TTL, renewal interval, etc.)
            api_url: Override API URL (defaults to config)
            timeout: HTTP timeout (defaults to config)
            workspace_id: Override workspace ID (defaults to config)
            api_key: Override API key (defaults to config)
        """
        # Owner ID: explicit > auto-generated
        self.owner_id = owner_id or str(uuid.uuid4())

        # Lock configuration
        self.config = config or RegistryLockManagerConfig()

        # HTTP client configuration (shared with APIRegistry)
        self._client_config = RegistryAPIClientConfig.from_config(
            api_url=api_url,
            timeout=timeout,
            workspace_id=workspace_id,
            api_key=api_key,
        )

        self._async_client: RegistryAPIAsyncHTTPClient | None = None

    @property
    def api_url(self) -> str:
        """Base API URL."""
        return self._client_config.api_url

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests."""
        return self._client_config.get_workspace_params()

    def _handle_response_error(
        self, response, operation: str = "Lock operation"
    ) -> None:
        """Check response for errors and raise appropriate exceptions."""
        # 423 (Locked), 429 (Too Many Requests), 409 (Conflict) are expected
        # lock-specific responses, not errors
        handle_response_error(
            response,
            operation=operation,
            workspace_id=self._client_config.workspace_id,
            ignore_status_codes=(423, 429, 409),
        )

    @property
    def async_client(self) -> RegistryAPIAsyncHTTPClient:
        """Lazy-initialized async HTTP client."""
        if self._async_client is None:
            self._async_client = get_async_http_client(self._client_config)
        return self._async_client

    def lock(self, task_id: str) -> LockHandle:
        """Get an async context manager for locking a task.

        Args:
            task_id: The task identifier (hash).

        Returns:
            A LockHandle that can be used as an async context manager.
        """
        return RegistryLockHandle(self, task_id, self.config)

    async def acquire(self, task_id: str) -> LockAcquisitionResult:
        """Acquire a lock for a task.

        Lower-level method. Prefer using lock() context manager which
        handles renewal and release automatically.

        Args:
            task_id: The task identifier (hash).

        Returns:
            LockAcquisitionResult with status.
        """
        return await self._acquire_internal(
            task_id,
            self.config.ttl_seconds,
            self.config.check_task_completion,
        )

    async def release(self, task_id: str, task_completed: bool = False) -> bool:
        """Release a lock.

        Lower-level method. Prefer using lock() context manager which
        handles renewal and release automatically.

        Args:
            task_id: The task identifier.
            task_completed: If True, record that the task completed successfully.

        Returns:
            True if successfully released, False otherwise.
        """
        return await self._release_internal(
            task_id,
            task_completed,
            self.config.build_id,
        )

    async def _acquire_internal(
        self,
        task_id: str,
        ttl_seconds: int,
        check_task_completion: bool,
    ) -> LockAcquisitionResult:
        """Internal acquire implementation."""
        try:
            response = await self.async_client.post(
                f"{self.api_url}/api/v1/locks/{task_id}/acquire",
                json={
                    "owner_id": self.owner_id,
                    "ttl_seconds": ttl_seconds,
                    "check_task_completion": check_task_completion,
                },
                params=self._get_params(),
            )

            if response.status_code == 423:
                data = response.json()
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.HELD_BY_OTHER,
                    acquired=False,
                    error_message=data.get("detail", {}).get("error_message"),
                )

            if response.status_code == 429:
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

    async def _renew_internal(self, task_id: str, ttl_seconds: int) -> bool:
        """Internal renew implementation."""
        try:
            response = await self.async_client.post(
                f"{self.api_url}/api/v1/locks/{task_id}/renew",
                json={
                    "owner_id": self.owner_id,
                    "ttl_seconds": ttl_seconds,
                },
                params=self._get_params(),
            )

            if response.status_code == 409:
                return False

            self._handle_response_error(response, f"Renew lock for {task_id}")

            data = response.json()
            return data.get("renewed", False)

        except Exception as e:
            logger.error(f"Failed to renew lock for {task_id}: {e}")
            return False

    async def _release_internal(
        self,
        task_id: str,
        task_completed: bool,
        build_id: str | None,
    ) -> bool:
        """Internal release implementation."""
        try:
            response = await self.async_client.post(
                f"{self.api_url}/api/v1/locks/{task_id}/release",
                json={
                    "owner_id": self.owner_id,
                    "task_completed": task_completed,
                    "build_id": build_id,
                },
                params=self._get_params(),
            )

            if response.status_code == 409:
                logger.warning(f"Failed to release lock for {task_id}: not owner")
                return False

            self._handle_response_error(response, f"Release lock for {task_id}")

            data = response.json()
            return data.get("released", False)

        except Exception as e:
            logger.error(f"Failed to release lock for {task_id}: {e}")
            return False

    async def aclose(self) -> None:
        """Close the async HTTP client."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def __aenter__(self) -> RegistryGlobalConcurrencyLockManager:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self.aclose()
        return False


# Type assertion to verify protocol compliance
def _check_protocol_compliance() -> None:
    """Static check that RegistryGlobalConcurrencyLockManager implements the protocol."""
    _manager: GlobalConcurrencyLockManager = RegistryGlobalConcurrencyLockManager()
    _handle: LockHandle = _manager.lock("task-id")
    # These would fail type checking if protocol isn't implemented correctly
    del _manager, _handle
