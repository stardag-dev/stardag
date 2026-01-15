"""Registry-based global concurrency lock implementation."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from stardag.build._base import (
    GlobalLockConfig,
    LockAcquisitionResult,
    LockAcquisitionStatus,
)
from stardag.registry._http_client import (
    RegistryAPIClientConfig,
    get_async_http_client,
    handle_response_error,
)

logger = logging.getLogger(__name__)


class RegistryLockHandle:
    """Lock handle with automatic TTL renewal.

    Implements the LockHandle protocol as an async context manager.
    Handles automatic renewal of the lock TTL while the task executes.
    """

    def __init__(
        self,
        manager: "RegistryGlobalConcurrencyLockManager",
        task_id: str,
        config: GlobalLockConfig,
    ) -> None:
        self._manager = manager
        self._task_id = task_id
        self._config = config
        self._result: LockAcquisitionResult | None = None
        self._task_completed = False
        self._renewal_task: asyncio.Task | None = None

    @property
    def result(self) -> LockAcquisitionResult:
        """The result of the lock acquisition attempt."""
        if self._result is None:
            raise RuntimeError("Lock not yet acquired - use async with")
        return self._result

    def mark_completed(self) -> None:
        """Mark that the task completed successfully.

        When called before exiting the context, the lock release will
        record task completion in the registry.
        """
        self._task_completed = True

    async def __aenter__(self) -> "RegistryLockHandle":
        """Acquire the lock with retry/backoff handling."""
        self._result = await self._manager._acquire_with_retry(
            self._task_id, self._config
        )

        # Start renewal task if lock was acquired
        if self._result.acquired:
            self._renewal_task = asyncio.create_task(
                self._renewal_loop(), name=f"lock-renewal-{self._task_id}"
            )

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Release the lock and stop renewal."""
        # Stop renewal task
        if self._renewal_task is not None:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
            self._renewal_task = None

        # Release lock if we acquired it
        if self._result is not None and self._result.acquired:
            # If exception occurred, don't mark as completed
            task_completed = self._task_completed and exc_type is None
            await self._manager.release(self._task_id, task_completed=task_completed)

        return False  # Don't suppress exceptions

    async def _renewal_loop(self) -> None:
        """Background task to periodically renew the lock."""
        # Renew at 50% of TTL to have safety margin
        ttl_seconds = 60  # Default TTL
        renewal_interval = ttl_seconds * 0.5

        while True:
            try:
                await asyncio.sleep(renewal_interval)
                renewed = await self._manager._renew(self._task_id, ttl_seconds)
                if not renewed:
                    logger.warning(
                        f"Failed to renew lock for task {self._task_id}, "
                        "lock may have been lost"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error renewing lock for task {self._task_id}: {e}")


class RegistryGlobalConcurrencyLockManager:
    """Global concurrency lock manager backed by the Stardag Registry API.

    Implements the GlobalConcurrencyLockManager protocol using HTTP calls to
    the stardag-api lock endpoints.

    The manager handles:
    - Owner ID generation (unique per build/instance)
    - Lock acquisition with exponential backoff
    - Automatic TTL renewal via LockHandle
    - Completion checking for eventual consistency

    Usage:
        lock_manager = RegistryGlobalConcurrencyLockManager()

        async with lock_manager.lock("task-id") as handle:
            if handle.result.acquired:
                # execute task
                handle.mark_completed()
            elif handle.result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                # skip - task already completed elsewhere
    """

    def __init__(
        self,
        owner_id: str | None = None,
        config: GlobalLockConfig | None = None,
        api_url: str | None = None,
        timeout: float | None = None,
        environment_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Initialize the lock manager.

        Args:
            owner_id: Unique identifier for this lock owner. If not provided,
                a random UUID will be generated. Should be stable across
                retries within the same build.
            config: Lock configuration (retry timeouts, backoff, etc.).
            api_url: Override for API URL (default from config).
            timeout: Override for HTTP timeout (default from config).
            environment_id: Override for workspace ID (default from config).
            api_key: Override for API key (default from config/env).
        """
        self.owner_id = owner_id or str(uuid4())
        self.config = config or GlobalLockConfig()

        self._client_config = RegistryAPIClientConfig.from_config(
            api_url=api_url,
            api_key=api_key,
            environment_id=environment_id,
            timeout=timeout,
        )
        self._async_client = None

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests (environment_id for JWT auth)."""
        cfg = self._client_config
        if cfg.access_token and not cfg.api_key and cfg.environment_id:
            return {"environment_id": cfg.environment_id}
        return {}

    @property
    def _client(self):
        """Lazy-initialized async HTTP client."""
        if self._async_client is None:
            self._async_client = get_async_http_client(self._client_config)
        return self._async_client

    def lock(self, task_id: str) -> RegistryLockHandle:
        """Get an async context manager for locking a task.

        Args:
            task_id: The task identifier (hash).

        Returns:
            A LockHandle that can be used as an async context manager.
            The handle's result indicates whether the lock was acquired.
        """
        return RegistryLockHandle(self, task_id, self.config)

    async def acquire(self, task_id: str) -> LockAcquisitionResult:
        """Acquire a lock for a task (without retry/backoff).

        Lower-level method - prefer using lock() context manager which
        includes retry logic and automatic renewal.

        Args:
            task_id: The task identifier (hash).

        Returns:
            LockAcquisitionResult with status.
        """
        return await self._acquire_internal(task_id, ttl_seconds=60)

    async def release(self, task_id: str, task_completed: bool = False) -> bool:
        """Release a lock.

        Args:
            task_id: The task identifier.
            task_completed: If True, record that the task completed successfully.

        Returns:
            True if successfully released, False otherwise.
        """
        try:
            response = await self._client.post(
                f"{self._client_config.api_url}/api/v1/locks/{task_id}/release",
                json={
                    "owner_id": self.owner_id,
                    "task_completed": task_completed,
                },
                params=self._get_params(),
            )

            if response.status_code == 409:
                logger.warning(f"Failed to release lock for {task_id}: not owner")
                return False

            handle_response_error(response, f"Release lock for {task_id}")
            data = response.json()
            return data.get("released", False)

        except Exception as e:
            logger.error(f"Failed to release lock for {task_id}: {e}")
            return False

    async def check_task_completed(self, task_id: str) -> bool:
        """Check if a task is registered as completed in the registry.

        Args:
            task_id: The task identifier.

        Returns:
            True if task has a completion record.
        """
        try:
            response = await self._client.get(
                f"{self._client_config.api_url}/api/v1/locks/tasks/{task_id}/completion-status",
                params=self._get_params(),
            )
            handle_response_error(response, f"Check completion for {task_id}")
            data = response.json()
            return data.get("is_completed", False)

        except Exception as e:
            logger.error(f"Failed to check completion for {task_id}: {e}")
            return False

    async def _acquire_internal(
        self,
        task_id: str,
        ttl_seconds: int = 60,
        check_task_completion: bool = True,
    ) -> LockAcquisitionResult:
        """Internal acquire method without retry logic."""
        try:
            response = await self._client.post(
                f"{self._client_config.api_url}/api/v1/locks/{task_id}/acquire",
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

            handle_response_error(response, f"Acquire lock for {task_id}")
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

    async def _acquire_with_retry(
        self,
        task_id: str,
        config: GlobalLockConfig,
    ) -> LockAcquisitionResult:
        """Acquire lock with exponential backoff retry.

        Handles HELD_BY_OTHER and CONCURRENCY_LIMIT_REACHED by waiting
        and retrying with exponential backoff.
        """
        timeout = config.lock_wait_timeout_seconds
        current_interval = config.lock_wait_initial_interval_seconds
        max_interval = config.lock_wait_max_interval_seconds
        backoff_factor = config.lock_wait_backoff_factor

        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while True:
            result = await self._acquire_internal(task_id)

            if result.status == LockAcquisitionStatus.ACQUIRED:
                return result

            if result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                return result

            if result.status == LockAcquisitionStatus.ERROR:
                return result

            # HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED - retry with backoff
            if timeout is None:
                # No waiting configured, fail immediately
                return result

            elapsed = loop.time() - start_time
            if elapsed >= timeout:
                return LockAcquisitionResult(
                    status=result.status,
                    acquired=False,
                    error_message=f"Timeout after {timeout}s: {result.status.value}",
                )

            logger.debug(
                f"Lock for {task_id} unavailable ({result.status}), "
                f"retrying in {current_interval:.1f}s..."
            )
            await asyncio.sleep(current_interval)
            current_interval = min(current_interval * backoff_factor, max_interval)

    async def _renew(self, task_id: str, ttl_seconds: int = 60) -> bool:
        """Renew a lock's TTL."""
        try:
            response = await self._client.post(
                f"{self._client_config.api_url}/api/v1/locks/{task_id}/renew",
                json={
                    "owner_id": self.owner_id,
                    "ttl_seconds": ttl_seconds,
                },
                params=self._get_params(),
            )

            if response.status_code == 409:
                return False

            handle_response_error(response, f"Renew lock for {task_id}")
            data = response.json()
            return data.get("renewed", False)

        except Exception as e:
            logger.error(f"Failed to renew lock for {task_id}: {e}")
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
