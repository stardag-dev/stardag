"""API-based registry that communicates with the stardag-api service."""

import asyncio
import gzip
import json as _json
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

import httpx
from httpx_retries import Retry, RetryTransport

from stardag.config import DEFAULT_API_TIMEOUT, config_provider
from stardag.registry._auth import StardagAPIKeyAuth, StardagTokenAuth
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
    is_missing_route_error,
)
from stardag.registry._base import (
    StartClaimResult,
    BuildFrontier,
    BuildInfo,
    RegisteredTaskInfo,
    RegistryABC,
    TaskMetadata,
    get_git_commit_hash,
)
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

# Hard cap on tasks per ``task_register_bulk[_aio]`` HTTP call. Mirrors
# the server's per-call cap.
#
# **Asymmetry intentional**: the build engine's
# ``_BULK_REGISTER_CHUNK_SIZE`` (50) is the *working* size — small
# enough to keep DB transactions short and request bodies friendly.
# The 1000 here is the *defensive* ceiling for direct external callers
# of ``APIRegistry`` who haven't been told about the lower working
# size. Don't reduce this thinking 50 is the real limit — it isn't.
_MAX_BULK_REGISTER_TASKS = 1000

# Threshold above which JSON request bodies are gzipped before sending.
# Below this, gzip's headers + per-call CPU make compression a net loss;
# above it, repeated keys / structure in our payloads (especially
# ``task_register_bulk``) compress 5–10×, so the wire savings dominate.
# The server transparently decompresses via ``GZipRequestMiddleware``.
_GZIP_REQUEST_THRESHOLD_BYTES = 1024


def _maybe_gzip_json_body(
    body: object,
) -> tuple[bytes | None, dict[str, str]]:
    """Serialize a JSON body and gzip it when worthwhile.

    Returns a ``(content_bytes, extra_headers)`` pair suitable for httpx's
    ``content=`` + ``headers=`` kwargs:

    - ``body is None`` -> ``(None, {})`` (caller skips the JSON body
      entirely).
    - body small (< ``_GZIP_REQUEST_THRESHOLD_BYTES``) -> raw JSON bytes
      + ``Content-Type: application/json``.
    - body big -> gzipped JSON bytes + ``Content-Type`` and
      ``Content-Encoding: gzip``.

    Always serialises with ``separators=(",", ":")`` so the size
    threshold doesn't depend on whitespace.
    """
    if body is None:
        return None, {}
    encoded = _json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if len(encoded) < _GZIP_REQUEST_THRESHOLD_BYTES:
        return encoded, headers
    headers["Content-Encoding"] = "gzip"
    return gzip.compress(encoded), headers


def _is_route_not_found(err: NotFoundError) -> bool:
    """Module-local alias for the shared missing-route check."""
    return is_missing_route_error(err)


def _parse_bulk_register_response(payload: object) -> "list[RegisteredTaskInfo] | None":
    """Parse the ``/tasks/bulk?id_only=true`` response into RegisteredTaskInfo.

    Tolerates older servers whose slim response carries only
    ``{id, task_id}`` — the execution-state fields then default to None and
    the build engine simply has nothing to re-attach to.
    """
    if not isinstance(payload, dict):
        return None
    items = payload.get("tasks")
    if not isinstance(items, list):
        return None
    infos: list[RegisteredTaskInfo] = []
    for item in items:
        if not isinstance(item, dict) or "task_id" not in item:
            return None
        infos.append(
            RegisteredTaskInfo(
                task_id=item["task_id"],
                latest_status=item.get("latest_status"),
                latest_executor=item.get("latest_executor"),
                latest_executor_ref=item.get("latest_executor_ref"),
                latest_executor_metadata=item.get("latest_executor_metadata"),
            )
        )
    return infos


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
        reg = config.registry

        # Resolve API URL: explicit > config
        resolved_url = api_url or (reg.url if reg else None)
        if not resolved_url:
            raise ValueError(
                "APIRegistry requires a registry URL. "
                "Set STARDAG_API_URL or configure a profile with a registry."
            )
        self.api_url = resolved_url.rstrip("/")

        # Timeout: explicit > config
        self.timeout = (
            timeout
            if timeout is not None
            else (reg.timeout if reg else DEFAULT_API_TIMEOUT)
        )

        # Environment ID: explicit > config
        self.environment_id = environment_id or (reg.environment_id if reg else None)

        # Build auth object
        resolved_api_key = api_key or (
            reg.auth.api_key.get_secret_value() if reg and reg.auth.api_key else None
        )
        if resolved_api_key:
            self._auth: httpx.Auth = StardagAPIKeyAuth(resolved_api_key)
            logger.debug("APIRegistry initialized with API key authentication")
        elif reg and reg.auth.access_token:
            # registry_name is optional — StardagTokenAuth can derive a
            # credential key from the URL when no profile is configured.
            self._auth = StardagTokenAuth(
                access_token=reg.auth.access_token.get_secret_value(),
                workspace_id=reg.workspace_id,
                user_email=reg.auth.user_email,
                registry_url=reg.url,
                registry_name=config.context.registry_name,
            )
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
            # No auth - pass None; httpx handles this gracefully
            self._auth = None  # type: ignore[assignment]
            logger.warning(
                "APIRegistry initialized without authentication. "
                "Run 'stardag auth login' or set STARDAG_API_KEY env var."
            )

        self._client = None
        self._async_client = None
        self._async_client_loop = (
            None  # Track which event loop the async client belongs to
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
        raw_detail = None
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
                f"{operation} failed",
                status_code=status_code,
                detail=detail,
                payload=raw_detail if isinstance(raw_detail, dict) else None,
            )

    @property
    def client(self):
        if self._client is None:
            transport = RetryTransport(retry=_RETRY_CONFIG)
            self._client = httpx.Client(
                timeout=self.timeout, auth=self._auth, transport=transport
            )
        return self._client

    def _get_params(self) -> dict[str, str]:
        """Get query params for API requests.

        When using JWT auth, environment_id must be passed as a query param.
        """
        if isinstance(self._auth, StardagTokenAuth) and self.environment_id:
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

        JSON bodies above ``_GZIP_REQUEST_THRESHOLD_BYTES`` are gzipped
        before sending; the server's ``GZipRequestMiddleware`` handles
        the decode transparently.

        Rate limit 429s (RATE_LIMIT error_code) are retried with backoff
        respecting the Retry-After header. Quota 429s (24h limits) are
        raised immediately as QuotaExceededError.
        """
        content, body_headers = _maybe_gzip_json_body(json)
        request_kwargs: dict[str, Any] = {"params": params}
        if content is not None:
            request_kwargs["content"] = content
            request_kwargs["headers"] = body_headers

        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = self.client.request(method, url, **request_kwargs)
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

        JSON bodies above ``_GZIP_REQUEST_THRESHOLD_BYTES`` are gzipped
        before sending; the server's ``GZipRequestMiddleware`` handles
        the decode transparently.

        Rate limit 429s (RATE_LIMIT error_code) are retried with backoff
        respecting the Retry-After header. Quota 429s (24h limits) are
        raised immediately as QuotaExceededError.
        """
        content, body_headers = _maybe_gzip_json_body(json)
        request_kwargs: dict[str, Any] = {"params": params}
        if content is not None:
            request_kwargs["content"] = content
            request_kwargs["headers"] = body_headers

        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = await self.async_client.request(method, url, **request_kwargs)
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
        executor_metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Start a new build and return its ID."""
        build_data: dict[str, Any] = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [str(task.id) for task in (root_tasks or [])],
            "description": description,
        }
        if executor_metadata is not None:
            # Only included when present — older servers ignore unknown
            # body fields anyway, but this keeps the payload minimal.
            build_data["executor_metadata"] = executor_metadata

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

    def build_resume(
        self,
        build_id: UUID,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark an existing build as resumed.

        Emits a BUILD_RESUMED event server-side so a build that previously
        terminated (FAILED / COMPLETED / CANCELLED / EXIT_EARLY) flips
        back to RUNNING. The endpoint is new in the post-resume API; on
        older servers the request 404s with FastAPI's missing-route body,
        which we swallow with a warning so the SDK keeps working against
        an un-upgraded registry. Resource-level 404s (build does not
        exist) are re-raised.
        """
        params = self._get_event_params()
        if executor_metadata is not None:
            params["executor_metadata"] = _json.dumps(
                executor_metadata, separators=(",", ":")
            )
        try:
            self._request(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/resume",
                params=params,
                operation="Resume build",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /builds/%s/resume; "
                "the resumed build will keep its previous status in the "
                "registry until you upgrade the API. Build will still run "
                "to completion locally.",
                build_id,
            )
            return
        logger.info(f"Resumed build: {build_id}")

    def build_complete(self, build_id: UUID) -> None:
        """Mark a build as completed."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/complete",
            params=self._get_event_params(),
            operation="Complete build",
        )
        logger.info(f"Completed build: {build_id}")

    def build_fail(self, build_id: UUID, error_message: str | None = None) -> None:
        """Mark a build as failed."""
        params = self._get_event_params()
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
            params=self._get_event_params(),
            operation="Cancel build",
        )
        logger.info(f"Cancelled build: {build_id}")

    def build_exit_early(self, build_id: UUID, reason: str | None = None) -> None:
        """Mark a build as exited early."""
        params = self._get_event_params()
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

    def task_register_bulk(
        self, build_id: UUID, tasks: Sequence["BaseTask"]
    ) -> list[RegisteredTaskInfo] | None:
        """Bulk-register tasks via the ``/tasks/bulk`` endpoint.

        Falls back to per-task ``task_register`` if the API doesn't
        support the endpoint (older deployments) — same backwards-compat
        pattern as ``task_add_dependencies``.

        Raises ``ValueError`` if the batch exceeds
        ``_MAX_BULK_REGISTER_TASKS`` (mirrors the server cap). The build
        engine chunks above this method; external callers of
        ``APIRegistry`` get an explicit client-side error rather than a
        400 from the server.

        Passes ``?id_only=true`` so the server returns only the
        ``{id, task_id}`` mapping rather than echoing back full
        ``TaskResponse`` rows we'd discard anyway. Cuts response size
        by ~10× for batches with rich task_data.
        """
        if not tasks:
            return None
        if len(tasks) > _MAX_BULK_REGISTER_TASKS:
            raise ValueError(
                f"task_register_bulk supports at most {_MAX_BULK_REGISTER_TASKS} "
                f"tasks per call (got {len(tasks)}). Chunk the input on the "
                f"caller side."
            )
        try:
            response = self._request(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/bulk",
                json={"tasks": [_get_task_data_for_registration(t) for t in tasks]},
                params={**self._get_params(), "id_only": "true"},
                operation=f"Bulk-register {len(tasks)} tasks",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /tasks/bulk; "
                "falling back to per-task registration. "
                "Upgrade the Registry API for batched registration."
            )
            for t in tasks:
                self.task_register(build_id, t)
            return None
        return _parse_bulk_register_response(response.json())

    def _get_event_params(self) -> dict[str, str]:
        """Get query params for event endpoints, including commit_hash."""
        params = self._get_params()
        try:
            params["commit_hash"] = get_git_commit_hash()
        except Exception:
            pass  # Git not available, skip commit_hash
        return params

    def task_start(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark a task as started.

        Caller must have already registered the task (via ``task_register`` or
        as a side effect of a parent's static-deps reconciliation). The /start
        endpoint will 404 otherwise.
        """
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/start",
            params=self._get_start_params(executor, executor_ref, executor_metadata),
            operation=f"Start task {task.id}",
        )

    def _get_start_params(
        self,
        executor: str | None,
        executor_ref: str | None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Event params plus optional detached-execution reference.

        ``executor_metadata`` rides as a JSON-encoded query param (the
        start endpoint has no body); older servers ignore the unknown
        param, so no version gating is needed.
        """
        params = self._get_event_params()
        if executor is not None:
            params["executor"] = executor
        if executor_ref is not None:
            params["executor_ref"] = executor_ref
        if executor_metadata is not None:
            params["executor_metadata"] = _json.dumps(
                executor_metadata, separators=(",", ":")
            )
        return params

    def task_complete(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as completed."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/complete",
            params=self._get_event_params(),
            operation=f"Complete task {task.id}",
        )

    def task_fail(
        self, build_id: UUID, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Mark a task as failed."""
        params = self._get_event_params()
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
            params=self._get_event_params(),
            operation=f"Suspend task {task.id}",
        )

    def task_add_dependencies(
        self,
        build_id: UUID,
        task: "BaseTask",
        upstream_tasks: Sequence["BaseTask"],
        is_dynamic: bool = True,
    ) -> None:
        """Record dependency edges for a task.

        Backward-compat: an older Registry API that lacks the
        ``/dependencies`` endpoint returns FastAPI's default 404 with the
        generic ``"Not Found"`` detail. We swallow that specific response
        with a warning so builds don't break on version skew. All other
        404s (e.g. our endpoint's explicit ``"Build not found"`` or
        ``"Task … not registered …"`` responses) re-raise normally.
        """
        if not upstream_tasks:
            return
        try:
            self._request(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/dependencies",
                json={
                    "upstream_task_ids": [str(u.id) for u in upstream_tasks],
                    "is_dynamic": is_dynamic,
                },
                params=self._get_params(),
                operation=f"Add dependencies for task {task.id}",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /dependencies; "
                "dynamic-dep edges for task %s will not be recorded. "
                "Upgrade the Registry API to see dynamic deps in the DAG view.",
                task.id,
            )

    def task_resume(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as resumed (dynamic dependencies completed)."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/resume",
            params=self._get_event_params(),
            operation=f"Resume task {task.id}",
        )

    def task_cancel(self, build_id: UUID, task: "BaseTask") -> None:
        """Cancel a task."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/cancel",
            params=self._get_event_params(),
            operation=f"Cancel task {task.id}",
        )

    def task_skip(self, build_id: UUID, task: "BaseTask") -> None:
        """Skip a task whose dependency failed or was cancelled.

        Backward-compat: an older Registry API that lacks the ``/skip``
        endpoint returns FastAPI's default 404 with the generic
        ``"Not Found"`` detail. We swallow that specific response with a
        warning so a new SDK against an old API doesn't fail builds on
        every fail-fast / blocked-dep path. All other 404s (e.g.
        ``"Build not found"``) re-raise normally.
        """
        try:
            self._request(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/skip",
                params=self._get_event_params(),
                operation=f"Skip task {task.id}",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /skip; task %s will "
                "remain PENDING in the registry. Upgrade the Registry API "
                "to see SKIPPED status for tasks blocked by failed deps.",
                task.id,
            )

    def task_waiting_for_lock(
        self, build_id: UUID, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Record that a task is waiting for a global lock."""
        params = self._get_event_params()
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

    # -------------------------------------------------------------------------
    # Named concurrency limits (per-environment)
    # -------------------------------------------------------------------------

    def concurrency_limit_list(self) -> list[dict[str, Any]]:
        """List the environment's named concurrency limits.

        Returns a list of ``{"key": str, "max_concurrent": int}`` dicts,
        ordered by key.
        """
        response = self._request(
            "GET",
            f"{self.api_url}/api/v1/concurrency-limits",
            params=self._get_params(),
            operation="List concurrency limits",
        )
        return list(response.json().get("limits", []))

    def concurrency_limit_set(self, key: str, max_concurrent: int) -> dict[str, Any]:
        """Create or update a named concurrency limit (PUT upsert).

        Returns the resulting ``{"key", "max_concurrent"}`` dict.
        """
        response = self._request(
            "PUT",
            f"{self.api_url}/api/v1/concurrency-limits/{quote(key, safe='')}",
            json={"max_concurrent": max_concurrent},
            params=self._get_params(),
            operation=f"Set concurrency limit {key!r}",
        )
        return response.json()

    def concurrency_limit_delete(self, key: str) -> None:
        """Delete a named concurrency limit (the key becomes unlimited)."""
        self._request(
            "DELETE",
            f"{self.api_url}/api/v1/concurrency-limits/{quote(key, safe='')}",
            params=self._get_params(),
            operation=f"Delete concurrency limit {key!r}",
        )

    def concurrency_limit_holders(self, key: str, limit: int = 100) -> dict[str, Any]:
        """List the RUNNING tasks currently holding slots of ``key``.

        Returns ``{"key", "holders": [...], "total": int}`` where each
        holder carries task id/name, ``latest_status_at`` (running since)
        and executor info. ``total`` is the full holder count (``holders``
        is capped by ``limit``, oldest-running first).
        """
        response = self._request(
            "GET",
            f"{self.api_url}/api/v1/concurrency-limits/{quote(key, safe='')}/holders",
            params={**self._get_params(), "limit": str(limit)},
            operation=f"List holders of concurrency limit {key!r}",
        )
        return response.json()

    def concurrency_limit_evict(self, key: str, task_id: str) -> dict[str, Any]:
        """Evict a RUNNING slot holder of ``key`` (records TASK_FAILED).

        Recovery path for slots leaked by a dead build process. Returns the
        ``{"task_id", "status"}`` event response.
        """
        response = self._request(
            "POST",
            f"{self.api_url}/api/v1/concurrency-limits/{quote(key, safe='')}"
            f"/holders/{quote(task_id, safe='')}/evict",
            params=self._get_params(),
            operation=f"Evict {task_id} from concurrency limit {key!r}",
        )
        return response.json()

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
                auth=self._auth,
                limits=limits,
                transport=transport,
            )
            self._async_client_loop = current_loop
        return self._async_client

    async def build_start_aio(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Async version - start a new build and return its ID."""
        build_data: dict[str, Any] = {
            "commit_hash": get_git_commit_hash(),
            "root_task_ids": [str(task.id) for task in (root_tasks or [])],
            "description": description,
        }
        if executor_metadata is not None:
            build_data["executor_metadata"] = executor_metadata

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

    async def build_resume_aio(
        self,
        build_id: UUID,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Async version - mark an existing build as resumed.

        See :meth:`build_resume` for the backward-compat 404 handling.
        """
        params = self._get_event_params()
        if executor_metadata is not None:
            params["executor_metadata"] = _json.dumps(
                executor_metadata, separators=(",", ":")
            )
        try:
            await self._arequest(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/resume",
                params=params,
                operation="Resume build",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /builds/%s/resume; "
                "the resumed build will keep its previous status in the "
                "registry until you upgrade the API. Build will still run "
                "to completion locally.",
                build_id,
            )
            return
        logger.info(f"Resumed build: {build_id}")

    async def build_complete_aio(self, build_id: UUID) -> None:
        """Async version - mark a build as completed."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/complete",
            params=self._get_event_params(),
            operation="Complete build",
        )
        logger.info(f"Completed build: {build_id}")

    async def build_fail_aio(
        self, build_id: UUID, error_message: str | None = None
    ) -> None:
        """Async version - mark a build as failed."""
        params = self._get_event_params()
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
            params=self._get_event_params(),
            operation="Cancel build",
        )
        logger.info(f"Cancelled build: {build_id}")

    async def build_exit_early_aio(
        self, build_id: UUID, reason: str | None = None
    ) -> None:
        """Async version - mark build as exited early."""
        params = self._get_event_params()
        if reason:
            params["reason"] = reason
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/exit-early",
            params=params,
            operation="Exit early",
        )
        logger.info(f"Build exited early: {build_id}")

    def build_list_running(
        self, limit: int = 100, reactive_app_name: str | None = None
    ) -> list[UUID]:
        """List ids of running builds (most recently active first).

        Pages through ``GET /builds``, narrowing server-side with
        ``status=running`` and (when given) ``reactive_app_name`` — the
        watchdog's real question is "RUNNING reactive builds owned by app
        X", and an unnarrowed listing lets unrelated builds consume
        ``limit`` (an environment that accumulates stale RUNNING builds
        would silently stop reaching the reactive ones).

        The derived status is re-checked client-side because a server
        predating either filter ignores unknown query params and answers
        with the unfiltered listing; the re-check keeps that degradation to
        "wider than asked for" rather than "wrong". A wider listing is
        harmless for the caller: a tick no-ops on a non-reactive build.
        """
        running: list[UUID] = []
        page = 1
        page_size = 100
        # Bound the sweep: builds are ordered by last_active_at desc, so
        # RUNNING builds cluster early — paging the entire history to find
        # stragglers would make the watchdog cost grow with total build
        # count. Truncation is logged.
        max_pages = 10
        filter_params = {"status": "running"}
        if reactive_app_name is not None:
            filter_params["reactive_app_name"] = reactive_app_name
        while len(running) < limit:
            response = self._request(
                "GET",
                f"{self.api_url}/api/v1/builds",
                params={
                    **self._get_params(),
                    **filter_params,
                    "page": str(page),
                    "page_size": str(page_size),
                },
                operation="List builds",
            )
            payload = response.json()
            builds = payload.get("builds", [])
            for build in builds:
                # Redundant against a server that honours ``status``, and
                # the only thing standing between a server that doesn't and
                # a watchdog ticking terminal builds — keep it.
                if build.get("status") == "running":
                    running.append(UUID(build["id"]))
                    if len(running) >= limit:
                        break
            if len(builds) < page_size:
                break
            if page >= max_pages:
                logger.warning(
                    f"build_list_running: stopped after {max_pages} pages "
                    f"({page * page_size} builds scanned); older running "
                    "builds (if any) are not included."
                )
                break
            page += 1
        return running

    def build_add_roots(self, build_id: UUID, root_task_ids: list[str]) -> None:
        """Append root task ids to a build."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/roots",
            json={"root_task_ids": root_task_ids},
            params=self._get_params(),
            operation=f"Add roots to build {build_id}",
        )

    async def build_add_roots_aio(
        self, build_id: UUID, root_task_ids: list[str]
    ) -> None:
        """Async version - append root task ids to a build."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/roots",
            json={"root_task_ids": root_task_ids},
            params=self._get_params(),
            operation=f"Add roots to build {build_id}",
        )

    def task_retry(self, build_id: UUID, task: "BaseTask") -> None:
        """Reset a failed/cancelled/skipped task to pending (retry)."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/retry",
            params=self._get_event_params(),
            operation=f"Retry task {task.id}",
        )

    async def task_retry_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - reset a failed/cancelled/skipped task to pending."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/retry",
            params=self._get_event_params(),
            operation=f"Retry task {task.id}",
        )

    def build_skip_blocked(self, build_id: UUID) -> list[str]:
        """Mark tasks transitively blocked by failures as skipped."""
        response = self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/skip-blocked",
            params=self._get_event_params(),
            operation=f"Skip blocked tasks in build {build_id}",
        )
        return list(response.json().get("skipped_task_ids", []))

    async def build_skip_blocked_aio(self, build_id: UUID) -> list[str]:
        """Async version - mark blocked tasks as skipped."""
        response = await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/skip-blocked",
            params=self._get_event_params(),
            operation=f"Skip blocked tasks in build {build_id}",
        )
        return list(response.json().get("skipped_task_ids", []))

    def build_notify(self, build_id: UUID) -> None:
        """Set the build's scheduler wake-up flag."""
        self._request(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/notify",
            params=self._get_params(),
            operation=f"Notify build {build_id}",
        )

    async def build_notify_aio(self, build_id: UUID) -> None:
        """Async version - set the build's scheduler wake-up flag."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/notify",
            params=self._get_params(),
            operation=f"Notify build {build_id}",
        )

    def build_clear_notify(self, build_id: UUID) -> None:
        """Clear the build's scheduler wake-up flag."""
        self._request(
            "DELETE",
            f"{self.api_url}/api/v1/builds/{build_id}/notify",
            params=self._get_params(),
            operation=f"Clear notify for build {build_id}",
        )

    async def build_clear_notify_aio(self, build_id: UUID) -> None:
        """Async version - clear the build's scheduler wake-up flag."""
        await self._arequest(
            "DELETE",
            f"{self.api_url}/api/v1/builds/{build_id}/notify",
            params=self._get_params(),
            operation=f"Clear notify for build {build_id}",
        )

    def build_get_frontier(self, build_id: UUID) -> BuildFrontier:
        """Return the build's scheduling frontier."""
        response = self._request(
            "GET",
            f"{self.api_url}/api/v1/builds/{build_id}/frontier",
            params=self._get_params(),
            operation=f"Get frontier for build {build_id}",
        )
        return BuildFrontier.model_validate(response.json())

    async def build_get_frontier_aio(self, build_id: UUID) -> BuildFrontier:
        """Async version - return the build's scheduling frontier."""
        response = await self._arequest(
            "GET",
            f"{self.api_url}/api/v1/builds/{build_id}/frontier",
            params=self._get_params(),
            operation=f"Get frontier for build {build_id}",
        )
        return BuildFrontier.model_validate(response.json())

    def build_get(self, build_id: UUID) -> BuildInfo:
        """Return a slim build record (lighter than the frontier)."""
        response = self._request(
            "GET",
            f"{self.api_url}/api/v1/builds/{build_id}",
            params=self._get_params(),
            operation=f"Get build {build_id}",
        )
        return BuildInfo.model_validate(response.json())

    async def build_get_aio(self, build_id: UUID) -> BuildInfo:
        """Async version - return a slim build record."""
        response = await self._arequest(
            "GET",
            f"{self.api_url}/api/v1/builds/{build_id}",
            params=self._get_params(),
            operation=f"Get build {build_id}",
        )
        return BuildInfo.model_validate(response.json())

    def build_set_reactive_meta(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Mark a build reactively scheduled and store its tick config (upsert).

        ``tick_kwargs=None`` (a bare re-trigger) is omitted from the request
        so the server preserves the stored config; pass a dict to update it.
        """
        try:
            self._request(
                "PUT",
                f"{self.api_url}/api/v1/builds/{build_id}/reactive-meta",
                json=self._reactive_meta_body(app_name, tick_kwargs),
                params=self._get_params(),
                operation=f"Set reactive meta for build {build_id}",
            )
        except NotFoundError as e:
            raise self._reactive_meta_unsupported_error(e)

    async def build_set_reactive_meta_aio(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Async version - mark a build reactively scheduled (upsert)."""
        try:
            await self._arequest(
                "PUT",
                f"{self.api_url}/api/v1/builds/{build_id}/reactive-meta",
                json=self._reactive_meta_body(app_name, tick_kwargs),
                params=self._get_params(),
                operation=f"Set reactive meta for build {build_id}",
            )
        except NotFoundError as e:
            raise self._reactive_meta_unsupported_error(e)

    @staticmethod
    def _reactive_meta_body(
        app_name: str, tick_kwargs: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Request body for PUT /reactive-meta; omit tick_kwargs when None.

        A None ``tick_kwargs`` means "leave the stored config untouched" — a
        bare re-trigger preserves the existing tick_kwargs rather than
        wiping them.
        """
        body: dict[str, Any] = {"app_name": app_name}
        if tick_kwargs is not None:
            body["tick_kwargs"] = tick_kwargs
        return body

    @staticmethod
    def _reactive_meta_unsupported_error(err: NotFoundError) -> Exception:
        """Turn a missing-route 404 into a clear reactive-unsupported error.

        Reactive scheduling requires a matching stardag-api version. When the
        server predates the reactive-meta endpoint the PUT 404s with the
        missing-route body — surface it as a clear error at the trigger (like
        the frontier/notify contract) rather than silently degrading. A
        resource-level 404 (build does not exist) is returned as-is.
        """
        if not _is_route_not_found(err):
            return err
        return RuntimeError(
            "The registry server does not support reactive scheduling "
            "(reactive-meta endpoint missing). Upgrade stardag-api to a "
            "version matching this SDK."
        )

    async def task_register_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - register a task within a build."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks",
            json=_get_task_data_for_registration(task),
            params=self._get_params(),
            operation=f"Register task {task.id}",
        )

    async def task_register_bulk_aio(
        self, build_id: UUID, tasks: Sequence["BaseTask"]
    ) -> list[RegisteredTaskInfo] | None:
        """Async bulk-register via ``/tasks/bulk`` (one HTTP call instead of N).

        Falls back to per-task ``task_register_aio`` if the API doesn't
        support the endpoint (older deployments).

        Raises ``ValueError`` if the batch exceeds
        ``_MAX_BULK_REGISTER_TASKS`` (mirrors the server cap). The build
        engine chunks above this method; external callers get an
        explicit client-side error rather than a 400 from the server.

        Passes ``?id_only=true`` so the server returns only the
        ``{id, task_id}`` mapping rather than echoing full ``TaskResponse``
        rows that we discard. Cuts response size by ~10× for batches
        with rich task_data.
        """
        if not tasks:
            return None
        if len(tasks) > _MAX_BULK_REGISTER_TASKS:
            raise ValueError(
                f"task_register_bulk_aio supports at most {_MAX_BULK_REGISTER_TASKS} "
                f"tasks per call (got {len(tasks)}). Chunk the input on the "
                f"caller side."
            )
        try:
            response = await self._arequest(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/bulk",
                json={"tasks": [_get_task_data_for_registration(t) for t in tasks]},
                params={**self._get_params(), "id_only": "true"},
                operation=f"Bulk-register {len(tasks)} tasks",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /tasks/bulk; "
                "falling back to per-task registration. "
                "Upgrade the Registry API for batched registration."
            )
            for t in tasks:
                await self.task_register_aio(build_id, t)
            return None
        return _parse_bulk_register_response(response.json())

    async def task_start_claim_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
        limit_keys: Sequence[str] | None = None,
    ) -> StartClaimResult:
        """Claiming start against the API (``claim=true`` on ``/start``).

        Maps the structured 409 denials (``task_already_running`` /
        ``task_already_completed`` / ``concurrency_limit_reached``) to a
        :class:`StartClaimResult`. Against an older server the ``claim``
        parameter is ignored and the start behaves like a plain (tolerated-
        duplicate) start — i.e. graceful degradation to pre-claim behavior.
        """
        params: dict[str, Any] = self._get_start_params(
            executor, executor_ref, executor_metadata
        )
        params["claim"] = "true"
        if limit_keys:
            params["limit_key"] = list(limit_keys)
            params["enforce_limits"] = "true"
        try:
            await self._arequest(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/start",
                params=params,
                operation=f"Start task {task.id} (claim)",
            )
        except APIError as e:
            payload = e.payload or {}
            error_code = payload.get("error_code")
            if e.status_code == 409 and error_code == "task_already_running":
                return StartClaimResult(
                    started=False,
                    denied_reason="already_running",
                    executor=payload.get("executor"),
                    executor_ref=payload.get("executor_ref"),
                    latest_status_at=payload.get("latest_status_at"),
                )
            if e.status_code == 409 and error_code == "task_already_completed":
                return StartClaimResult(
                    started=False, denied_reason="already_completed"
                )
            if e.status_code == 409 and error_code == "concurrency_limit_reached":
                return StartClaimResult(
                    started=False,
                    denied_reason="limit",
                    denied_keys=list(payload.get("denied_keys") or []),
                )
            raise
        return StartClaimResult(started=True)

    async def task_start_with_limits_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
        limit_keys: Sequence[str] | None = None,
    ) -> bool:
        """Start a task with atomic server-side concurrency-limit acquisition.

        Sends ``limit_key`` (repeated) + ``enforce_limits=true``; a 409 with
        error code ``concurrency_limit_reached`` means a key was at capacity
        — returns False without recording anything.
        """
        params: dict[str, Any] = self._get_start_params(
            executor, executor_ref, executor_metadata
        )
        if limit_keys:
            params["limit_key"] = list(limit_keys)
            params["enforce_limits"] = "true"
        try:
            await self._arequest(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/start",
                params=params,
                operation=f"Start task {task.id} (limits)",
            )
        except APIError as e:
            if e.status_code == 409 and "concurrency_limit_reached" in (e.detail or ""):
                return False
            raise
        return True

    async def task_start_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Async version - mark a task as started.

        Caller must have already registered the task (via ``task_register_aio``
        or as a side effect of a parent's static-deps reconciliation). The
        /start endpoint will 404 otherwise.
        """
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/start",
            params=self._get_start_params(executor, executor_ref, executor_metadata),
            operation=f"Start task {task.id}",
        )

    async def task_complete_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - mark a task as completed."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/complete",
            params=self._get_event_params(),
            operation=f"Complete task {task.id}",
        )

    async def task_fail_aio(
        self, build_id: UUID, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Async version - mark a task as failed."""
        params = self._get_event_params()
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
            params=self._get_event_params(),
            operation=f"Suspend task {task.id}",
        )

    async def task_add_dependencies_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        upstream_tasks: Sequence["BaseTask"],
        is_dynamic: bool = True,
    ) -> None:
        """Async version - record dependency edges for a task.

        Same backward-compat behavior as the sync version: only swallow
        the specific "missing route" 404 (FastAPI default ``"Not Found"``);
        re-raise genuine resource-not-found 404s.
        """
        if not upstream_tasks:
            return
        try:
            await self._arequest(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/dependencies",
                json={
                    "upstream_task_ids": [str(u.id) for u in upstream_tasks],
                    "is_dynamic": is_dynamic,
                },
                params=self._get_params(),
                operation=f"Add dependencies for task {task.id}",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /dependencies; "
                "dynamic-dep edges for task %s will not be recorded. "
                "Upgrade the Registry API to see dynamic deps in the DAG view.",
                task.id,
            )

    async def task_resume_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - mark a task as resumed."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/resume",
            params=self._get_event_params(),
            operation=f"Resume task {task.id}",
        )

    async def task_cancel_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - cancel a task."""
        await self._arequest(
            "POST",
            f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/cancel",
            params=self._get_event_params(),
            operation=f"Cancel task {task.id}",
        )

    async def task_skip_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version - skip a task whose dep failed or was cancelled.

        See :meth:`task_skip` for the backward-compat 404 handling.
        """
        try:
            await self._arequest(
                "POST",
                f"{self.api_url}/api/v1/builds/{build_id}/tasks/{task.id}/skip",
                params=self._get_event_params(),
                operation=f"Skip task {task.id}",
            )
        except NotFoundError as e:
            if not _is_route_not_found(e):
                raise
            logger.warning(
                "Registry API does not support POST /skip; task %s will "
                "remain PENDING in the registry. Upgrade the Registry API "
                "to see SKIPPED status for tasks blocked by failed deps.",
                task.id,
            )

    async def task_waiting_for_lock_aio(
        self, build_id: UUID, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Async version - record that task is waiting for global lock."""
        params = self._get_event_params()
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
