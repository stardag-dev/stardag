"""Stardag SDK exceptions.

This module provides exception classes for API and authentication errors,
with clear error messages that can be propagated to CLI output.
"""


class StardagError(Exception):
    """Base exception for all Stardag SDK errors."""

    pass


class APIError(StardagError):
    """Error communicating with the Stardag API.

    Attributes:
        status_code: HTTP status code (if available)
        detail: Error detail message from the API
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
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


class WorkspaceAccessError(AuthorizationError):
    """Not authorized to access the specified workspace."""

    def __init__(self, workspace_id: str | None = None, detail: str | None = None):
        msg = "Not authorized to access this workspace"
        if workspace_id:
            msg = f"Not authorized to access workspace '{workspace_id}'"
        super().__init__(msg, detail=detail)


class NotFoundError(APIError):
    """Resource not found (404)."""

    def __init__(
        self,
        message: str = "Resource not found",
        detail: str | None = None,
    ):
        super().__init__(message, status_code=404, detail=detail)
