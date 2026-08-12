"""Stardag SDK exceptions.

This module provides exception classes for API and authentication errors,
with clear error messages that can be propagated to CLI output.
"""


class StardagError(Exception):
    """Base exception for all Stardag SDK errors."""

    pass


class TaskInterrupted(StardagError):
    """This attempt ended for a reason unrelated to the task's correctness.

    Raise this from a task that caught a platform interruption — a
    preemption signal, or the execution backend's function timeout — and
    persisted whatever progress it had. It says "run me again", and the
    execution layer arranges for that to happen.

    The canonical shape, for a task that is expected to be killed and
    resumed until it converges::

        class TrainModel(sd.Task[Model]):
            def run(self):
                try:
                    train(resume_from=self.checkpoint_path())
                except KeyboardInterrupt:      # the preemption signal
                    save_checkpoint(self.checkpoint_path())
                    raise sd.TaskInterrupted("checkpointed") from None

    **Why this exists at all**, given that a bare ``raise`` in that
    ``except`` block would also work: the failure mode it replaces is
    silent and expensive. An interrupt reaches user code as a
    ``BaseException``, so it escapes ``except Exception`` — which is what
    lets the execution layer see the container as crashed and re-run the
    input. Catch the interrupt to checkpoint, re-raise anything derived
    from ``Exception``, and that same task becomes a permanent failure
    instead, taking a ``FAIL_FAST`` build with it. One keyword apart, no
    warning. Naming the intent makes the correct behaviour survive the
    later refactor that wraps the body in a broad ``except``.

    Deliberately an ``Exception`` and not a ``BaseException``: it is raised
    from inside the user's own error handling, where a ``BaseException``
    subclass would be one more thing that slips past their control flow for
    no benefit. The runner catches ``BaseException`` regardless.
    """


class TaskPreempted(TaskInterrupted):
    """The execution backend reclaimed the container running this task."""


class TaskTimedOut(TaskInterrupted):
    """The execution backend's function timeout ended this attempt.

    Distinct from :class:`TaskPreempted` because the two want different
    defaults: a container reclaimed mid-run says nothing about the task,
    while a task that hits its timeout may equally be hung. Which reading
    applies is a property of the task, configured on the scheduler — see
    ``TickConfig.interruption_policy_selector``.
    """


class APIError(StardagError):
    """Error communicating with the Stardag API.

    Attributes:
        status_code: HTTP status code (if available)
        detail: Error detail message from the API
        payload: The structured error detail (the API's ``detail`` dict)
            when the response carried one — lets callers branch on
            ``error_code`` and read machine-readable fields without
            string matching.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        detail: str | None = None,
        payload: dict | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.payload = payload
        # Build a clear message
        parts = [message]
        if status_code:
            parts.append(f"(HTTP {status_code})")
        if detail:
            parts.append(f": {detail}")
        super().__init__(" ".join(parts))


class AuthenticationError(APIError):
    """Authentication failed.

    This is raised when:
    - Token is expired
    - Token is invalid
    - Token is missing required claims
    - API key is invalid
    - No authentication provided
    """

    def __init__(
        self,
        message: str = "Authentication failed",
        status_code: int | None = 401,
        detail: str | None = None,
    ):
        super().__init__(message, status_code, detail)


class TokenExpiredError(AuthenticationError):
    """Access token has expired.

    Re-authenticate with 'stardag auth login' to get a new token.
    """

    def __init__(self, detail: str | None = None):
        super().__init__(
            "Access token has expired. Run 'stardag auth login' to re-authenticate.",
            status_code=401,
            detail=detail,
        )


class InvalidTokenError(AuthenticationError):
    """Access token is invalid.

    The token may be malformed or have invalid claims.
    Re-authenticate with 'stardag auth login' to get a new token.
    """

    def __init__(self, detail: str | None = None):
        super().__init__(
            "Access token is invalid. Run 'stardag auth login' to re-authenticate.",
            status_code=401,
            detail=detail,
        )


class InvalidAPIKeyError(AuthenticationError):
    """API key is invalid.

    The API key may have been revoked or doesn't exist.
    """

    def __init__(self, detail: str | None = None):
        super().__init__(
            "API key is invalid. Check your STARDAG_API_KEY or create a new key.",
            status_code=401,
            detail=detail,
        )


class NotAuthenticatedError(AuthenticationError):
    """No authentication credentials provided.

    Either run 'stardag auth login' or set the STARDAG_API_KEY environment variable.
    """

    def __init__(self, detail: str | None = None):
        super().__init__(
            "Not authenticated. Run 'stardag auth login' or set STARDAG_API_KEY.",
            status_code=401,
            detail=detail,
        )


class AuthorizationError(APIError):
    """Authorization failed (403 Forbidden).

    You don't have permission to access this resource.
    """

    def __init__(
        self,
        message: str = "Access denied",
        detail: str | None = None,
    ):
        super().__init__(message, status_code=403, detail=detail)


class EnvironmentAccessError(AuthorizationError):
    """Not authorized to access the specified environment."""

    def __init__(self, environment_id: str | None = None, detail: str | None = None):
        msg = "Not authorized to access this environment"
        if environment_id:
            msg = f"Not authorized to access environment '{environment_id}'"
        super().__init__(msg, detail=detail)


class NotFoundError(APIError):
    """Resource not found (404)."""

    def __init__(
        self,
        message: str = "Resource not found",
        detail: str | None = None,
    ):
        super().__init__(message, status_code=404, detail=detail)


def is_missing_route_error(err: "NotFoundError") -> bool:
    """Distinguish FastAPI's default "missing route" 404 from app-level 404s.

    FastAPI serves unknown paths as ``{"detail": "Not Found"}``. Any 404
    raised inside an endpoint (``raise HTTPException(status_code=404,
    detail=...)``) carries a more specific detail string (e.g.
    ``"Build not found"``), so checking the exact ``"Not Found"`` literal is
    a reliable way to tell "endpoint doesn't exist on this server" apart from
    "this particular resource doesn't exist". Used to convert a genuine
    missing-endpoint 404 into a clear "server too old" error without
    misreporting a legitimate resource-level 404.
    """
    return getattr(err, "detail", None) == "Not Found"


class SDKVersionUnsupportedError(APIError):
    """This SDK is older than the registry's minimum supported version (426).

    Raised when the server answers ``426 Upgrade Required`` to the
    ``X-Stardag-SDK-Version`` this SDK sends on every request. The server
    knows both versions *and* the exact upgrade command, so it composes the
    user-facing sentence; we carry it through verbatim rather than
    paraphrasing it — a paraphrase is a second source of truth that starts
    drifting the day the server's wording changes.

    ``sdk_version`` / ``minimum_sdk_version`` are the same two versions in
    machine-readable form, for callers that want to branch rather than print.

    Attributes:
        message: The server's sentence, unadorned — what a UI should show.
            ``str(exc)`` is the same text with ``(HTTP 426)`` appended by
            :class:`APIError`.
        sdk_version: The version this SDK reported, as the server saw it.
        minimum_sdk_version: The oldest version the server accepts.
    """

    # Only used if a 426 somehow arrives without the structured detail —
    # the server always sends one, so this is a floor, not the normal path.
    _FALLBACK_MESSAGE = (
        "This Stardag registry requires a newer stardag SDK than the one "
        'installed. Upgrade with: pip install --upgrade "stardag"'
    )

    def __init__(
        self,
        message: str | None = None,
        sdk_version: str | None = None,
        minimum_sdk_version: str | None = None,
        payload: dict | None = None,
    ):
        self.message = message or self._FALLBACK_MESSAGE
        self.sdk_version = sdk_version
        self.minimum_sdk_version = minimum_sdk_version
        super().__init__(
            self.message,
            status_code=426,
            detail=None,
            payload=payload,
        )


class RateLimitError(APIError):
    """Per-minute rate limit exceeded (retryable).

    The SDK will automatically retry with backoff. If you see this error
    propagated, the retry budget was exhausted.
    """

    def __init__(self, retry_after: int, detail: str | None = None):
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded (retry after {retry_after}s)",
            status_code=429,
            detail=detail,
        )


class QuotaExceededError(APIError):
    """24-hour entity creation quota exceeded (not retryable).

    Contact info@stardag.com to request a higher quota.
    """

    def __init__(self, detail: str | None = None):
        super().__init__("Quota exceeded", status_code=429, detail=detail)
