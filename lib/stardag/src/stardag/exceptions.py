"""Stardag SDK exceptions.

This module provides exception classes for API and authentication errors,
with clear error messages that can be propagated to CLI output.
"""


class StardagError(Exception):
    """Base exception for all Stardag SDK errors."""

    pass


class ResumableInterruption(StardagError):
    """Raise this to say: I saved my progress, run me again.

    The **only** way a task asks to be resumed. Raise it after catching a
    platform interruption — the execution backend reclaiming your container,
    or hitting its function timeout — and persisting whatever progress you
    had::

        class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
            def target(self) -> sd.DirectoryTarget:
                return sd.get_directory_target(sd.get_default_relpath(self))

            def run(self):
                directory = self.target()
                checkpoint = directory / "checkpoint.json"
                try:
                    train(resume_from=checkpoint)
                except MODAL_INTERRUPTIONS:
                    save_checkpoint(checkpoint)
                    raise sd.ResumableInterruption("checkpointed") from None
                directory.mark_done()

    **An interruption you do NOT catch is a failure**, deliberately. Letting
    one propagate means the task had no plan for it: either it hung, or the
    worker's timeout is too small for the work. Both want the same answer —
    fail, with the scheduler's ordinary attempt budget — and neither is
    improved by running it again twenty times. So there is no configuration
    deciding "is this timeout expected?": the task answers that by whether
    it raises this, and a task not built to resume simply never does.

    Catch the interruption types **specifically** (see
    ``stardag.integration.modal.MODAL_INTERRUPTIONS``), never
    ``BaseException``. A blanket catch would sweep up ordinary bugs — a
    ``NameError`` is a ``BaseException`` too — and turn a deterministic
    failure into a task that resumes until its budget runs out.

    Deliberately an ``Exception``, not a ``BaseException``: you raise it
    from inside your own error handling, where a ``BaseException`` subclass
    would be one more thing slipping past your control flow.

    What the Modal runner does with it depends on whether a restart is
    still possible, which it reads off **the interruption you caught** —
    still reachable from the exception you raise, and ``raise ... from
    None`` keeps it there (that form hides the "During handling…" preamble;
    it does not discard the original).

    Caught a *preemption*, the runner re-raises an interrupt in its place so
    the backend sees a crashed container and restarts the input on the same
    call id, and records the preemption so a restart that never arrives is
    visible. Caught a *function timeout* or a *cancel* — when no restart is
    coming — it records an interruption for a scheduler to act on instead,
    and lets your exception propagate unchanged.

    So raise this from inside the ``except`` block. Raised where the
    original interruption cannot be reached from, the runner falls back to
    comparing elapsed time against the worker's declared ``timeout``, which
    is a guess on a clock that starts after the container does.
    """


def execution_not_wanted(error: "APIError") -> bool:
    """Whether the registry refused a report because this execution is over.

    ``execution_not_current``: the task's claim has moved on from this
    execution (taken over after it lapsed, closed by an observation, or
    released by its build's terminal transition). ``execution_superseded``:
    a claiming start named an execution whose claim has already ended.
    ``unknown_execution``: the registry never recorded it. ``not_claim_holder``:
    the report came through a plan other than the one holding the claim.
    Either way, *stop: this container is not what the task is waiting for*.

    Matched on the code rather than the status, because 409 also carries
    the claim denials and conflicts, which mean different things.
    """
    if error.status_code != 409:
        return False
    return error.code in EXECUTION_OVER_CODES


#: The refusal codes :func:`execution_not_wanted` reads as "this execution
#: is over".
EXECUTION_OVER_CODES = frozenset(
    {
        "execution_not_current",
        "execution_superseded",
        "unknown_execution",
        "not_claim_holder",
    }
)


class ExecutionCancelled(StardagError):
    """Raise this to stop an execution the build no longer wants.

    Raised by stardag itself at the two automatic cooperative-cancellation
    checkpoints — the start of each attempt, and each dynamic-dependency
    yield — and available to a task that finds a safe stopping point of
    its own::

        def run(self):
            for chunk in self.chunks():
                if sd.cancellation_requested():
                    raise sd.ExecutionCancelled()
                process(chunk)

    The worker treats it as a **clean exit, not a failure of the task**:
    it writes no output, reports no completion, and records no
    end-of-attempt event. What it does *not* do is return normally, and
    that is deliberate — a Modal call that succeeds with no output written
    is read by a scheduler's probe as "the worker wrote it, eventual
    consistency", which would record a completion for a target that does
    not exist. Letting it propagate makes the call fail, which is honest
    and recoverable.

    An ordinary ``Exception``, like
    :class:`ResumableInterruption`, so it does not slip past a task's own
    error handling. A task that catches it should re-raise.

    Not something to raise speculatively: ask
    :func:`stardag.cancellation_requested` first, which is throttled and
    answers False unless the registry positively said this execution has
    been superseded or its build has stopped running.
    """


class UnstableSerializationError(StardagError):
    """A task object's instance body is not a fixed point of its own round
    trip: ``dump(validate(dump(x))) != dump(x)``, or the rehydrated task
    object has a different task id, or the body is not JSON at all (a
    ``NaN``).

    The instance hash is the hash of the body, so a body that moves when
    re-read would register one instance from the trigger and a different
    one from any process that rehydrates it. Raised at registration, before
    anything is sent, naming the field(s) whose value moved as dotted paths
    (``inner.when``) in :attr:`fields`.
    """

    def __init__(self, message: str, *, task_class: str, fields: tuple[str, ...]):
        super().__init__(message)
        self.task_class = task_class
        self.fields = fields


class InstanceConflictError(StardagError):
    """Two constructions of one task id with different instance hashes in one
    discovery pass: two ways of asking for one completion in one plan, of
    which a plan may hold only one.

    Typically two downstreams passing different values of a
    ``significant=False`` parameter to one upstream. :attr:`fields` names
    the fields that differ between the two instance bodies (dotted paths);
    :attr:`paths` holds how each construction was reached, where the
    discovery walk knows it.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str,
        fields: tuple[str, ...],
        paths: tuple[str | None, str | None] = (None, None),
    ):
        super().__init__(message)
        self.task_id = task_id
        self.fields = fields
        self.paths = paths


class APIError(StardagError):
    """Error communicating with the Stardag API.

    Attributes:
        status_code: HTTP status code (if available)
        detail: Error detail message from the API
        payload: The structured error detail (the API's ``detail`` dict,
            ``{"code", "message", ...}``) when the response carried one.
        code: ``payload["code"]`` — the machine-readable reason a caller
            branches on (``plan_superseded``, ``task_already_running``,
            ...), never the status alone.
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

    @property
    def code(self) -> str | None:
        """The registry's refusal code (``detail.code``), if it sent one."""
        value = (self.payload or {}).get("code")
        return value if isinstance(value, str) else None


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
        payload: dict | None = None,
    ):
        super().__init__(message, status_code=404, detail=detail, payload=payload)


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
