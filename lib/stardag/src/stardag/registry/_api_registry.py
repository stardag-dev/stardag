"""API-based registry that communicates with the stardag-api service."""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from httpx_retries import Retry, RetryTransport

from stardag.config import config_provider
from stardag.exceptions import (
    APIError,
    AuthorizationError,
    EnvironmentAccessError,
    InvalidAPIKeyError,
    InvalidTokenError,
    NotAuthenticatedError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    TokenExpiredError,
)
from stardag.registry._base import RegistryABC, TaskMetadata, get_git_commit_hash
from stardag.artifact import Artifact

if TYPE_CHECKING:
    from stardag._core.base_task import BaseTask

logger = logging.getLogger(__name__)

# Retry configuration for transient errors (connection issues, timeouts, etc.)
# Retries on: TimeoutException, NetworkError (includes ReadError), RemoteProtocolError
_RETRY_CONFIG = Retry(
    total=3,
    backoff_factor=0.5,
    # Also retry POST since our API calls are idempotent (task state transitions)
    allowed_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE"],
)

# Rate limit retry configuration
_MAX_RATE_LIMIT_RETRIES = 5
_MAX_RETRY_WAIT = 60  # Cap wait time at 60 seconds


class APIRegistry(RegistryABC):
    """Registry that stores task information via the stardag-api REST service.

    This registry is stateless with respect to build_id - the build_id is passed
    explicitly to all methods that need it. This allows a single registry instance
    to be reused across multiple builds (via registry_provider).

    Usage:
        build_id = await registry.build_start_aio(root_tasks=tasks)
        await registry.task_register_aio(build_id, task)
        await registry.task_start_aio(build_id, task)
        # ... execute task ...
        await registry.task_complete_aio(build_id, task)
        await registry.build_complete_aio(build_id)

    Authentication:
    - API key can be provided directly or via STARDAG_API_KEY env var
    - JWT token from browser login (stored in registry credentials)

    Configuration is loaded from the central config module (stardag.config).
    """

    def __init__(
        self,
        api_url: str | None = None,
        timeout: float | None = None,
        environment_id: str | None = None,
        api_key: str | None = None,
    ):
        # Load central config
        config = config_provider.get()

        # API key: explicit > config (which includes env var)
        self.api_key = api_key or config.api_key

        # Access token from config (browser login, only if no API key)
        self.access_token = config.access_token if not self.api_key else None

        # API URL: explicit > config
        resolved_url = api_url or config.api.url
        if not resolved_url:
            raise ValueError(
                "APIRegistry requires a registry URL. "
                "Set STARDAG_REGISTRY_URL or configure a profile with a registry."
            )
        self.api_url = resolved_url.rstrip("/")

        # Timeout: explicit > config
        self.timeout = timeout if timeout is not None else config.api.timeout

        # Environment ID: explicit > config
        self.environment_id = environment_id or config.context.environment_id

        self._client = None
        self._async_client = None
        self._async_client_loop = (
            None  # Track which event loop the async client belongs to
        )

        if self.api_key:
            logger.debug("APIRegistry initialized with API key authentication")
        elif self.access_token:
            if not self.environment_id:
                logger.warning(
                    "APIRegistry: JWT auth requires environment_id. "
                    "Run 'stardag config set environment <id>' to set it."
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
            EnvironmentAccessError: If environment access denied
            AuthorizationError: If other 403 error
            NotFoundError: If resource not found
            RateLimitError: If per-minute rate limit exceeded (retryable)
            QuotaExceededError: If 24h quota exceeded (not retryable)
            APIError: For other HTTP errors
        """
        if response.status_code < 400:
            return  # No error

        # Try to extract detail from response JSON
        detail = None
        error_code = None
        try:
            data = response.json()
            raw_detail = data.get("detail")
            if isinstance(raw_detail, dict):
                error_code = raw_detail.get("error_code")
                detail = raw_detail.get("message", str(raw_detail))
            else:
                detail = raw_detail if raw_detail else str(data)
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
            if "environment" in detail_lower:
                raise EnvironmentAccessError(
                    environment_id=self.environment_id, detail=detail
                )
            else:
                raise AuthorizationError(f"{operation} access denied", detail=detail)

        elif status_code == 404:
            raise NotFoundError(f"{operation}: resource not found", detail=detail)

        elif status_code == 429:
            if error_code == "RATE_LIMIT":
                retry_after = int(response.headers.get("Retry-After", 1))
                raise RateLimitError(retry_after=retry_after, detail=detail)
            else:
                raise QuotaExceededError(detail=detail)

        else:
            raise APIError(
                f"{operation} failed", status_code=status_code, detail=detail
            )

    @property
    def client(self):
        if self._client is None:
            # Create client with appropriate auth header and retry transport
            headers = {}
            if self.api_key:
                # API key auth (production/CI)
                headers["X-API-Key"] = self.api_key
            elif self.access_token:
                # JWT auth from browser login (local dev)
                headers["Authorization"] = f"Bearer {self.access_token}"
            transport = RetryTransport(retry=_RETRY_CONFIG)
            self._client = httpx.Client(
                timeout=self.timeout, headers=headers, transport=transport
            )
        return self._client

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests.

        When using JWT auth, environment_id must be passed as a query param.
        """
        if self.access_token and not self.api_key and self.environment_id:
            return {"environment_id": self.environment_id}
        return {}

    # -------------------------------------------------------------------------
    # Request helpers with rate-limit retry
    # -------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: object = None,
        params: dict[str, str] | None = None,
        operation: str = "API call",
    ) -> httpx.Response:
        """Make a sync HTTP request with automatic rate-limit retry.

        Rate limit 429s (RATE_LIMIT error_code) are retried with backoff
        respecting the Retry-After header. Quota 429s (24h limits) are
        raised immediately as QuotaExceededError.
        """
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = self.client.request(method, url, json=json, params=params)
            try:
                self._handle_response_error(response, operation)
                return response
            except RateLimitError as e:
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "Rate limit retry budget exhausted for %s after %d attempts",
                        operation,
                        attempt + 1,
                    )
                    raise
                wait = min(e.retry_after, _MAX_RETRY_WAIT)
                logger.warning(
                    "Rate limited on %s (attempt %d/%d), retrying in %ds...",
                    operation,
                    attempt + 1,
                    _MAX_RATE_LIMIT_RETRIES,
                    wait,
                )
                time.sleep(wait)
        # Unreachable, but satisfies type checker
        return response  # type: ignore[possibly-undefined]

    async def _arequest(
        self,
        method: str,
        url: str,
        *,
        json: object = None,
        params: dict[str, str] | None = None,
        operation: str = "API call",
    ) -> httpx.Response:
        """Make an async HTTP request with automatic rate-limit retry.

        Rate limit 429s (RATE_LIMIT error_code) are retried with backoff
        respecting the Retry-After header. Quota 429s (24h limits) are
        raised immediately as QuotaExceededError.
        """
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = await self.async_client.request(
                method, url, json=json, params=params
            )
            try:
                self._handle_response_error(response, operation)
                return response
            except RateLimitError as e:
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "Rate limit retry budget exhausted for %s after %d attempts",
                        operation,
                        attempt + 1,
                    )
                    raise
                wait = min(e.retry_after, _MAX_RETRY_WAIT)
                logger.warning(
                    "Rate limited on %s (attempt %d/%d), retrying in %ds...",
                    operation,
                    attempt + 1,
                    _MAX_RATE_LIMIT_RETRIES,
                    wait,
                )
                await asyncio.sleep(wait)
        # Unreachable, but satisfies type checker
        return response  # type: ignore[possibly-undefined]

    # -------------------------------------------------------------------------
    # Sync build methods
    # -------------------------------------------------------------------------

    def build_start(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> UUID:
        """Start a new build and return its ID."""
        build_data = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [str(task.id) for task in (root_tasks or [])],
            "description": description,
        }

        response = self._request(
            "POST",
            f"{self.api_url}/api/v1/builds",
            json=build_data,
            params=self._get_params(),
            operation="Start build",
        )
        data = response.json()
        build_id = UUID(data["id"])
        logger.info(f"Started build: {data['name']} (ID: {build_id})")
        return build_id

    def build_complete(self, build_id: UUID) -> None:
        """Mark a build as completed."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/complete",
            params=self._get_params(),
            operation="Complete build",
        )
        logger.info(f"Completed build: {build_id}")

    def build_fail(self, build_id: UUID, error_message: str | None = None) -> None:
        """Mark a build as failed."""
        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/fail",
            params=params,
            operation="Fail build",
        )
        logger.info(f"Marked build as failed: {build_id}")

    def build_cancel(self, build_id: UUID) -> None:
        """Cancel a build."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/cancel",
            params=self._get_params(),
            operation="Cancel build",
        )
        logger.info(f"Cancelled build: {build_id}")

    def build_exit_early(self, build_id: UUID, reason: str | None = None) -> None:
        """Mark a build as exited early."""
        params = self._get_params()
        if reason:
            params["reason"] = reason
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/exit-early",
            params=params,
            operation="Exit early",
        )
        logger.info(f"Build exited early: {build_id}")

    # -------------------------------------------------------------------------
    # Sync task methods
    # -------------------------------------------------------------------------

    def task_register(self, build_id: UUID, task: "BaseTask") -> None:
        """Register a task within a build."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks",
            json=_get_task_data_for_registration(task),
            params=self._get_params(),
            operation=f"Register task {task.id}",
        )

    def task_start(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as started."""
        # Ensure task is registered first
        self.task_register(build_id, task)

        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/start",
            params=self._get_params(),
            operation=f"Start task {task.id}",
        )

    def task_complete(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as completed."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/complete",
            params=self._get_params(),
            operation=f"Complete task {task.id}",
        )

    def task_fail(
        self, build_id: UUID, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Mark a task as failed."""
        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/fail",
            params=params,
            operation=f"Fail task {task.id}",
        )

    def task_suspend(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as suspended (waiting for dynamic dependencies)."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/suspend",
            params=self._get_params(),
            operation=f"Suspend task {task.id}",
        )

    def task_resume(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as resumed (dynamic dependencies completed)."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/resume",
            params=self._get_params(),
            operation=f"Resume task {task.id}",
        )

    def task_cancel(self, build_id: UUID, task: "BaseTask") -> None:
        """Cancel a task."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/cancel",
            params=self._get_params(),
            operation=f"Cancel task {task.id}",
        )

    def task_waiting_for_lock(
        self, build_id: UUID, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Record that a task is waiting for a global lock."""
        params = self._get_params()
        if lock_owner:
            params["lock_owner"] = lock_owner
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/waiting-for-lock",
            params=params,
            operation=f"Task {task.id} waiting for lock",
        )

    def task_upload_artifacts(
        self, build_id: UUID, task: "BaseTask", artifacts: Sequence[Artifact]
    ) -> None:
        """Upload artifacts for a completed task."""
        if not artifacts:
            return

        # Serialize artifacts to API format
        # For all artifact types, body is stored as a dict in body_json
        # - markdown: {"content": "<markdown string>"}
        # - json: the actual JSON data dict
        artifacts_data = []
        for artifact in artifacts:
            data = artifact.model_dump(mode="json")
            if artifact.type == "markdown":
                # Wrap markdown body string in {"content": ...} dict
                data["body"] = {"content": data["body"]}
            artifacts_data.append(data)

        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/artifacts",
            json=artifacts_data,
            params=self._get_params(),
            operation=f"Upload artifacts for task {task.id}",
        )
        logger.debug(f"Uploaded {len(artifacts)} artifacts for task {task.id}")

    def task_get_metadata(self, task_id: UUID) -> TaskMetadata:
        """Get metadata for a registered task.

        Args:
            task_id: The UUID of the task to get metadata for.

        Returns:
            A TaskMetadata object containing task metadata.
        """

        response = self._request(
            "GET",
            f"{self.api_url}/api/v1/tasks/{task_id}/metadata",
            params=self._get_params(),
            operation=f"Get metadata for task {task_id}",
        )
        data = response.json()

        return TaskMetadata.model_validate(data)

    # -------------------------------------------------------------------------
    # Client lifecycle
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Async client and methods
    # -------------------------------------------------------------------------

    @property
    def async_client(self):
        """Lazy-initialized async HTTP client with retry transport.

        The client is recreated if the event loop changes, which can happen
        when running in frameworks like Prefect that create new event loops
        for task execution.
        """

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        # Recreate client if loop changed or client doesn't exist
        if self._async_client is None or self._async_client_loop != current_loop:
            # Close old client if it exists
            old_client = self._async_client
            if old_client is not None:
                # Schedule close on the old loop if possible, otherwise just discard
                try:
                    if self._async_client_loop and self._async_client_loop.is_running():
                        self._async_client_loop.call_soon_threadsafe(
                            lambda c=old_client: asyncio.create_task(c.aclose())
                        )
                except Exception:
                    pass  # Best effort cleanup

            headers = {}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            elif self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            # Use limits to prevent stale connection issues
            # keepalive_expiry=5 closes idle connections after 5 seconds
            limits = httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=5,
            )
            transport = RetryTransport(retry=_RETRY_CONFIG)
            self._async_client = httpx.AsyncClient(
                timeout=self.timeout,
                headers=headers,
                limits=limits,
                transport=transport,
            )
            self._async_client_loop = current_loop
        return self._async_client

    async def build_start_aio(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> UUID:
        """Async version - start a new build and return its ID."""
        build_data = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [str(task.id) for task in (root_tasks or [])],
            "description": description,
        }

        response = await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds",
            json=build_data,
            params=self._get_params(),
            operation="Start build",
        )
        data = response.json()
        build_id = UUID(data["id"])
        logger.info(f"Started build: {data['name']} (ID: {build_id})")
        return build_id

    async def build_complete_aio(self, build_id: UUID) -> None:
        """Async version - mark a build as completed."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/complete",
            params=self._get_params(),
            operation="Complete build",
        )
        logger.info(f"Completed build: {build_id}")

    async def build_fail_aio(
        self, build_id: UUID, error_message: str | None = None
    ) -> None:
        """Async version - mark a build as failed."""
        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/fail",
            params=params,
            operation="Fail build",
        )
        logger.info(f"Marked build as failed: {build_id}")

    async def build_cancel_aio(self, build_id: UUID) -> None:
        """Async version - cancel a build."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/cancel",
            params=self._get_params(),
            operation="Cancel build",
        )
        logger.info(f"Cancelled build: {build_id}")

    async def build_exit_early_aio(
        self, build_id: UUID, reason: str | None = None
    ) -> None:
        """Async version - mark build as exited early."""
        params = self._get_params()
        if reason:
            params["reason"] = reason
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/exit-early",
            params=params,
            operation="Exit early",
        )
        logger.info(f"Build exited early: {build_id}")

    async def task_register_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - register a task within a build."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks",
            json=_get_task_data_for_registration(task),
            params=self._get_params(),
            operation=f"Register task {task.id}",
        )

    async def task_start_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - mark a task as started."""
        await self.task_register_aio(build_id, task)

        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/start",
            params=self._get_params(),
            operation=f"Start task {task.id}",
        )

    async def task_complete_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - mark a task as completed."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/complete",
            params=self._get_params(),
            operation=f"Complete task {task.id}",
        )

    async def task_fail_aio(
        self, build_id: UUID, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Async version - mark a task as failed."""
        params = self._get_params()
        if error_message:
            params["error_message"] = error_message
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/fail",
            params=params,
            operation=f"Fail task {task.id}",
        )

    async def task_suspend_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - mark a task as suspended."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/suspend",
            params=self._get_params(),
            operation=f"Suspend task {task.id}",
        )

    async def task_resume_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - mark a task as resumed."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/resume",
            params=self._get_params(),
            operation=f"Resume task {task.id}",
        )

    async def task_cancel_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - cancel a task."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/cancel",
            params=self._get_params(),
            operation=f"Cancel task {task.id}",
        )

    async def task_waiting_for_lock_aio(
        self, build_id: UUID, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Async version - record that task is waiting for global lock."""
        params = self._get_params()
        if lock_owner:
            params["lock_owner"] = lock_owner
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/waiting-for-lock",
            params=params,
            operation=f"Task {task.id} waiting for lock",
        )

    async def task_upload_artifacts_aio(
        self, build_id: UUID, task: "BaseTask", artifacts: Sequence[Artifact]
    ) -> None:
        """Async version - upload artifacts for a completed task."""
        if not artifacts:
            return

        artifacts_data = []
        for artifact in artifacts:
            data = artifact.model_dump(mode="json")
            if artifact.type == "markdown":
                data["body"] = {"content": data["body"]}
            artifacts_data.append(data)

        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/artifacts",
            json=artifacts_data,
            params=self._get_params(),
            operation=f"Upload artifacts for task {task.id}",
        )
        logger.debug(f"Uploaded {len(artifacts)} artifacts for task {task.id}")

    async def task_get_metadata_aio(self, task_id: UUID) -> TaskMetadata:
        """Async version of task_get_metadata."""

        response = await self._arequest(
            "GET",
            f"{self.api_url}/api/v1/tasks/{task_id}/metadata",
            params=self._get_params(),
            operation=f"Get metadata for task {task_id}",
        )
        data = response.json()

        return TaskMetadata.model_validate(data)


def _get_task_data_for_registration(task: "BaseTask") -> dict:
    """Helper to serialize task data for registration API call."""
    # Avoid circular import:
    from stardag._core.base_task import flatten_task_struct  # noqa: F401

    # Extract output_uri if the task has a FileSystemTarget target with a uri
    output_uri: str | None = None
    try:
        target_method = getattr(task, "target", None)
        if target_method is not None:
            target = target_method()
            if hasattr(target, "uri"):
                output_uri = target.uri
    except Exception as e:
        # Log but don't fail - task may not have target() or it may fail
        logger.debug(f"Could not extract output_uri for task {task.id}: {e}")

    return {
        "task_id": str(task.id),
        "task_namespace": task.get_namespace(),
        "task_name": task.get_name(),
        "task_data": task.model_dump(mode="json"),
        "version": task.version,
        "output_uri": output_uri,
        "dependency_task_ids": [
            str(dep.id) for dep in flatten_task_struct(task.requires())
        ],
    }
