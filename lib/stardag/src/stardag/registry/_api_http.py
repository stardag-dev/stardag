"""HTTP transport of :class:`~stardag.registry.APIRegistry`.

Everything that is about talking to the registry and nothing about what is
said: the base URL, authentication, the retrying sync and async clients,
gzip for large bodies, the retry of a lost exchange, rate-limit backoff,
and the mapping of error responses to :mod:`stardag.exceptions`.

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
import collections
import gzip
import json as _json
import logging
import platform
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import httpx

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

# A lost exchange is retried for every method: a request that never got a
# complete answer -- no connection, no headers in time, or a body that
# stalled or was cut short -- and an answer the app did not write (a
# gateway error from the proxy in front of it).
#
# Retrying a write rests on re-delivery being safe, which holds for almost
# every v2 route: ids are client-minted, inserts are DO NOTHING, lifecycle
# transitions are idempotent by state, a retried claiming start by the same
# execution is granted, and a retried yield batch is replayed by its
# ``batch_id`` (design.md, "Registration"). Not for all of them, and the
# exceptions are known rather than assumed away: a re-delivered execution
# report (complete, fail, ...) whose first delivery landed is refused with
# ``execution_already_ended``; ``POST /builds/wake-candidates`` is a drain,
# so a re-delivery hands out a different set; and ``build_create`` without
# a ``build_id`` creates a second build. Each surfaces as an error or a
# delayed wake-up, never as wrong state, and each was already exposed to
# the header-phase retry this loop replaces (STA-54).
#
# The retry wraps the whole exchange, body read included. A retry inside
# the httpx transport cannot: the transport returns once the headers are
# in, and the body is read after it has returned, so a stall there raised
# straight to the caller.
#
# Bounded twice: by a number of retries, and by time -- no retry starts
# once the call has been going for longer than two timeouts. Two, not
# one: a timed-out attempt has by definition taken one full timeout, and
# the retry exists for exactly that case. So a call that times out gets at
# least one more attempt, and waits at most about three timeouts rather
# than four -- a renewal should not block far past the claim or lease it
# is renewing.
_MAX_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_SECONDS = 0.5
_TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
_GATEWAY_STATUSES = frozenset({502, 503, 504})
# Modal's web proxy answers 500 with a plain-text body of its own when it
# loses the request on its way to or from the container ("modal-http:
# internal error: ... Server has lost track of input"). The app never
# writes that prefix -- its errors are JSON, or Starlette's plain
# "Internal Server Error" for an unhandled one -- so it tells the two apart.
_PROXY_ERROR_PREFIX = "modal-http:"

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


_retry_counts: collections.Counter[str] = collections.Counter()
_retry_counts_lock = threading.Lock()


def transport_retry_counts() -> dict[str, int]:
    """How many exchanges this process has retried, by cause.

    A cause is an exception class name (``ReadTimeout``), ``HTTP <status>``,
    or ``HTTP 500 (proxy)`` for the proxy's own error page.
    Every retry is also logged as a warning; this is the same tally in a form
    a harness can read without parsing logs.
    """
    with _retry_counts_lock:
        return dict(_retry_counts)


def _transient_cause(response: httpx.Response) -> str | None:
    """Why ``response`` is not the app's answer, or None if it is."""
    status = response.status_code
    if status in _GATEWAY_STATUSES:
        return f"HTTP {status}"
    if status == 500 and response.text.lstrip().startswith(_PROXY_ERROR_PREFIX):
        return f"HTTP {status} (proxy)"
    return None


def _note_retry(
    request: Request[Any], cause: str, detail: str, retry: int, delay: float
) -> None:
    with _retry_counts_lock:
        _retry_counts[cause] += 1
    logger.warning(
        "Registry %s %s got no complete answer (%s: %s); retrying in %.1fs "
        "(retry %d of %d).",
        request.method,
        request.path,
        cause,
        detail,
        delay,
        retry,
        _MAX_TRANSIENT_RETRIES,
    )


def _transient_delay(retry: int) -> float:
    """Exponential backoff with full jitter: up to 0.5 s, 1 s, 2 s.

    Jittered because the callers are many: every worker and tick meeting
    the same stall would otherwise re-send in lockstep.
    """
    return random.uniform(0, _TRANSIENT_BACKOFF_SECONDS * 2 ** (retry - 1))


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

    def _may_retry(self, retries: int, started: float) -> bool:
        return (
            retries < _MAX_TRANSIENT_RETRIES
            and time.monotonic() - started < 2 * self.timeout
        )

    def call(self, request: Request[T]) -> T:
        url, kwargs = self._kwargs(request)
        transient = rate_limited = 0
        started = time.monotonic()
        while True:
            try:
                response = self.client.request(request.method, url, **kwargs)
            except _TRANSIENT_EXCEPTIONS as e:
                if not self._may_retry(transient, started):
                    raise
                transient += 1
                delay = _transient_delay(transient)
                _note_retry(request, type(e).__name__, str(e), transient, delay)
                time.sleep(delay)
                continue
            cause = _transient_cause(response)
            if cause is not None and self._may_retry(transient, started):
                transient += 1
                delay = _transient_delay(transient)
                _note_retry(request, cause, response.text[:120], transient, delay)
                time.sleep(delay)
                continue
            try:
                self.raise_for_error(response, request.operation)
            except RateLimitError as e:
                if rate_limited >= _MAX_RATE_LIMIT_RETRIES:
                    raise
                rate_limited += 1
                time.sleep(min(e.retry_after, _MAX_RETRY_WAIT))
                continue
            return request.parse(_json_or_none(response))

    async def acall(self, request: Request[T]) -> T:
        url, kwargs = self._kwargs(request)
        transient = rate_limited = 0
        started = time.monotonic()
        while True:
            try:
                response = await self.async_client.request(
                    request.method, url, **kwargs
                )
            except _TRANSIENT_EXCEPTIONS as e:
                if not self._may_retry(transient, started):
                    raise
                transient += 1
                delay = _transient_delay(transient)
                _note_retry(request, type(e).__name__, str(e), transient, delay)
                await asyncio.sleep(delay)
                continue
            cause = _transient_cause(response)
            if cause is not None and self._may_retry(transient, started):
                transient += 1
                delay = _transient_delay(transient)
                _note_retry(request, cause, response.text[:120], transient, delay)
                await asyncio.sleep(delay)
                continue
            try:
                self.raise_for_error(response, request.operation)
            except RateLimitError as e:
                if rate_limited >= _MAX_RATE_LIMIT_RETRIES:
                    raise
                rate_limited += 1
                await asyncio.sleep(min(e.retry_after, _MAX_RETRY_WAIT))
                continue
            return request.parse(_json_or_none(response))

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
