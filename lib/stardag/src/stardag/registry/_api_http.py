"""HTTP transport of :class:`~stardag.registry.APIRegistry`.

Everything that is about talking to the registry and nothing about what is
said: the base URL, authentication, the retrying sync and async clients,
gzip for large bodies, rate-limit backoff, and the mapping of error
responses to :mod:`stardag.exceptions`.

A route is described once, as a :class:`Request` (method, path, body, query
and a parser for the answer), and sent by :meth:`HTTPTransport.call` or
:meth:`HTTPTransport.acall` — so the sync and async method of one route
cannot drift apart.

Every v2 refusal carries ``{"detail": {"code", "message", ...}}``; the code
is what callers branch on (:attr:`APIError.code`), never the status alone —
409 carries claim denials, late reports and conflicts, which mean different
things to different callers.
"""

from __future__ import annotations

import asyncio
import gzip
import json as _json
import logging
import platform
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import httpx
from httpx_retries import Retry, RetryTransport

from stardag._version import __version__
from stardag.config import DEFAULT_API_TIMEOUT, config_provider
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
from stardag.registry._auth import StardagAPIKeyAuth, StardagTokenAuth

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v2"

# Sent on every request. v2 has no version gate: the SDK and the registry
# are one release line and are upgraded together, so this identifies the
# client in logs and nothing reads it as a contract.
SDK_CLIENT_HEADERS = {
    "User-Agent": (
        f"stardag/{__version__} "
        f"(Python/{platform.python_version()}; httpx/{httpx.__version__})"
    ),
}

# Connection-pool limits for the async client, sized for a *process*: the
# client is cached per event loop on a process-wide registry, and a Modal
# container serves several scheduler ticks on one loop.
_ASYNC_MAX_CONNECTIONS = 100
_ASYNC_MAX_KEEPALIVE_CONNECTIONS = 50

# Transient transport errors (connection, timeout, protocol) are retried for
# every method. That is sound for POST because every v2 write is idempotent
# on re-delivery: ids are client-minted, inserts are DO NOTHING, lifecycle
# transitions are idempotent by state, a retried claiming start by the same
# execution is granted, and a retried yield batch is replayed by its
# ``batch_id`` (design.md, "Registration").
_RETRY_CONFIG = Retry(
    total=3,
    backoff_factor=0.5,
    allowed_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE"],
)

_MAX_RATE_LIMIT_RETRIES = 5
_MAX_RETRY_WAIT = 60

# Above this, a JSON body is gzipped (the server's GZipRequestMiddleware
# decodes it): registration chunks compress 5-10x.
_GZIP_REQUEST_THRESHOLD_BYTES = 1024

T = TypeVar("T")

QueryParams = dict[str, str] | Sequence[tuple[str, str]]


@dataclass(frozen=True)
class Request(Generic[T]):
    """One route call: what to send, and how to read the answer."""

    method: str
    path: str
    parse: Callable[[Any], T]
    json: Any = None
    params: dict[str, str] = field(default_factory=dict)
    operation: str = "API call"


def gzip_json_body(body: object) -> tuple[bytes | None, dict[str, str]]:
    """Serialize a JSON body, gzipped when worthwhile: ``(content, headers)``."""
    if body is None:
        return None, {}
    encoded = _json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if len(encoded) < _GZIP_REQUEST_THRESHOLD_BYTES:
        return encoded, headers
    headers["Content-Encoding"] = "gzip"
    return gzip.compress(encoded), headers


def _json_or_none(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


class HTTPTransport:
    """The registry's HTTP plumbing. See the module docstring."""

    def __init__(
        self,
        api_url: str | None = None,
        timeout: float | None = None,
        environment_id: str | None = None,
        api_key: str | None = None,
    ):
        config = config_provider.get()
        reg = config.registry
        resolved_url = api_url or (reg.url if reg else None)
        if not resolved_url:
            raise ValueError(
                "APIRegistry requires a registry URL. "
                "Set STARDAG_API_URL or configure a profile with a registry."
            )
        self.api_url = resolved_url.rstrip("/")
        self.timeout = (
            timeout
            if timeout is not None
            else (reg.timeout if reg else DEFAULT_API_TIMEOUT)
        )
        self.environment_id = environment_id or (reg.environment_id if reg else None)

        resolved_api_key = api_key or (
            reg.auth.api_key.get_secret_value() if reg and reg.auth.api_key else None
        )
        self._auth: httpx.Auth | None
        if resolved_api_key:
            self._auth = StardagAPIKeyAuth(resolved_api_key)
        elif reg and reg.auth.access_token:
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
            self._auth = None
            logger.warning(
                "APIRegistry initialized without authentication. "
                "Run 'stardag auth login' or set STARDAG_API_KEY env var."
            )
        self._client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._async_client_loop: asyncio.AbstractEventLoop | None = None

    # -- clients -----------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                auth=self._auth,
                transport=RetryTransport(retry=_RETRY_CONFIG),
                headers=SDK_CLIENT_HEADERS,
            )
        return self._client

    @property
    def async_client(self) -> httpx.AsyncClient:
        """The async client of the running loop, rebuilt when the loop
        changes (frameworks such as Prefect run tasks on fresh loops)."""
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._async_client is None or self._async_client_loop is not current_loop:
            old_client = self._async_client
            if old_client is not None:
                try:
                    if self._async_client_loop and self._async_client_loop.is_running():
                        self._async_client_loop.call_soon_threadsafe(
                            lambda c=old_client: asyncio.ensure_future(c.aclose())
                        )
                except Exception:
                    pass
            self._async_client = httpx.AsyncClient(
                timeout=self.timeout,
                auth=self._auth,
                limits=httpx.Limits(
                    max_connections=_ASYNC_MAX_CONNECTIONS,
                    max_keepalive_connections=_ASYNC_MAX_KEEPALIVE_CONNECTIONS,
                    keepalive_expiry=5,
                ),
                transport=RetryTransport(retry=_RETRY_CONFIG),
                headers=SDK_CLIENT_HEADERS,
            )
            self._async_client_loop = current_loop
        return self._async_client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
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

    # -- requests ----------------------------------------------------------------

    def _base_params(self) -> dict[str, str]:
        """With JWT auth the environment travels as a query parameter."""
        if isinstance(self._auth, StardagTokenAuth) and self.environment_id:
            return {"environment_id": self.environment_id}
        return {}

    def _kwargs(self, request: Request[Any]) -> tuple[str, dict[str, Any]]:
        content, headers = gzip_json_body(request.json)
        kwargs: dict[str, Any] = {"params": {**self._base_params(), **request.params}}
        if content is not None:
            kwargs["content"] = content
            kwargs["headers"] = headers
        return f"{self.api_url}{API_PREFIX}{request.path}", kwargs

    def call(self, request: Request[T]) -> T:
        url, kwargs = self._kwargs(request)
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = self.client.request(request.method, url, **kwargs)
            try:
                self.raise_for_error(response, request.operation)
            except RateLimitError as e:
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    raise
                time.sleep(min(e.retry_after, _MAX_RETRY_WAIT))
                continue
            return request.parse(_json_or_none(response))
        raise AssertionError("unreachable")  # pragma: no cover

    async def acall(self, request: Request[T]) -> T:
        url, kwargs = self._kwargs(request)
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = await self.async_client.request(request.method, url, **kwargs)
            try:
                self.raise_for_error(response, request.operation)
            except RateLimitError as e:
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    raise
                await asyncio.sleep(min(e.retry_after, _MAX_RETRY_WAIT))
                continue
            return request.parse(_json_or_none(response))
        raise AssertionError("unreachable")  # pragma: no cover

    def raise_for_error(self, response: httpx.Response, operation: str) -> None:
        """Map an error response to the SDK's exceptions (no-op below 400).

        The structured ``detail`` (``{"code", "message", ...}``) is kept on
        the exception as ``payload``; :attr:`APIError.code` reads it.
        """
        status_code = response.status_code
        if status_code < 400:
            return
        payload: dict[str, Any] | None = None
        detail: str | None
        try:
            raw = response.json().get("detail")
        except Exception:
            raw = None
            detail = response.text[:200] if response.text else None
        else:
            if isinstance(raw, dict):
                payload = raw
                detail = str(raw.get("message") or raw)
            elif raw is None:
                detail = response.text[:200] if response.text else None
            else:
                detail = str(raw)

        if status_code == 401:
            lowered = (detail or "").lower()
            if "expired" in lowered:
                raise TokenExpiredError(detail)
            if "api key" in lowered:
                raise InvalidAPIKeyError(detail)
            if "not authenticated" in lowered or not detail:
                raise NotAuthenticatedError(detail)
            raise InvalidTokenError(detail)
        if status_code == 403:
            if "environment" in (detail or "").lower():
                raise EnvironmentAccessError(
                    environment_id=self.environment_id, detail=detail
                )
            raise AuthorizationError(f"{operation} access denied", detail=detail)
        if status_code == 404:
            raise NotFoundError(
                f"{operation}: resource not found", detail=detail, payload=payload
            )
        if status_code == 429:
            if (payload or {}).get("error_code") == "RATE_LIMIT" or (payload or {}).get(
                "code"
            ) == "RATE_LIMIT":
                raise RateLimitError(
                    retry_after=int(response.headers.get("Retry-After", 1)),
                    detail=detail,
                )
            raise QuotaExceededError(detail=detail)
        raise APIError(
            f"{operation} failed",
            status_code=status_code,
            detail=detail,
            payload=payload,
        )
