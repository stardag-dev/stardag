"""Authentication routes: token exchange, JWKS, and local (email/password) auth."""

import logging
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.dependencies import (
    get_current_user_flexible,
    session_invalidated_by_password_change,
)
from stardag_api.auth.jwt import (
    AuthenticationError,
    TokenPayload,
    get_jwt_validator,
)
from stardag_api.auth.passwords import (
    PasswordPolicyError,
    hash_password_async,
    validate_password_policy,
    verify_password_async,
)
from stardag_api.auth.tokens import (
    TokenError,
    get_jwks,
    get_token_manager,
)
from stardag_api.config import auth_settings, oidc_settings
from stardag_api.db import get_db
from stardag_api.models import WorkspaceMember, User
from stardag_api.models.base import utc_now
from stardag_api.services.local_auth import (
    authenticate_local_user,
    create_local_user,
    login_email_rate_limiter,
    login_rate_limiter,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# Bearer scheme for OIDC tokens (only used by /auth/exchange)
oidc_bearer = HTTPBearer(auto_error=True)


class AuthConfigResponse(BaseModel):
    """Authentication configuration for SDK/CLI and UI clients.

    Served at runtime so clients (in particular the web UI) can discover how
    to authenticate against this registry without baking config in at build
    time. In "local" auth mode the OIDC fields are null and clients should
    use email/password login instead.
    """

    auth_mode: Literal["oidc", "local"] = "oidc"
    oidc_issuer: str | None = None
    # Client ID for SDK/CLI clients (kept as `oidc_client_id` for
    # backwards compatibility with existing SDK versions)
    oidc_client_id: str | None = None
    # Client ID the web UI should use
    oidc_ui_client_id: str | None = None
    # Cognito hosted-UI domain, only set for Cognito (non-standard logout)
    cognito_domain: str | None = None
    # Whether self-service signup is enabled (local mode only)
    local_registration_enabled: bool = False


class TokenExchangeRequest(BaseModel):
    """Request body for token exchange."""

    workspace_id: UUID


class TokenExchangeResponse(BaseModel):
    """Response from token exchange."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds


# Light format check only (full RFC validation adds a dependency for no
# practical gain here - the address is just the login identifier)
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class LoginRequest(BaseModel):
    """Request body for local-auth login."""

    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255)
    password: str


class RegisterRequest(BaseModel):
    """Request body for local-auth registration."""

    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255)
    password: str
    display_name: str | None = None


class SessionTokenResponse(BaseModel):
    """Response from local-auth login/registration."""

    session_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds


class ChangePasswordRequest(BaseModel):
    """Request body for password change (local auth)."""

    current_password: str
    new_password: str


async def get_oidc_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(oidc_bearer)],
) -> TokenPayload:
    """Validate OIDC JWT and return payload.

    This is only used by the /auth/exchange endpoint.
    All other endpoints use internal tokens.
    """
    validator = get_jwt_validator()
    try:
        return await validator.validate_token(credentials.credentials)
    except AuthenticationError as e:
        logger.warning("OIDC token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_or_create_user(db: AsyncSession, token: TokenPayload) -> User:
    """Get existing user or create new one from OIDC token claims."""
    # Look up user by external_id (OIDC subject claim)
    result = await db.execute(select(User).where(User.external_id == token.sub))
    user = result.scalar_one_or_none()

    if user is not None:
        # Update user info if changed
        updated = False
        if token.email and user.email != token.email:
            user.email = token.email
            updated = True
        if token.display_name and user.display_name != token.display_name:
            user.display_name = token.display_name
            updated = True
        if updated:
            await db.commit()
            logger.info("Updated user %s with new info from token", user.id)
        return user

    # Create new user
    if not token.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token must contain email claim for user creation",
        )

    user = User(
        external_id=token.sub,
        email=token.email,
        display_name=token.display_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Created new user %s from OIDC token", user.id)
    return user


@router.get("/.well-known/jwks.json")
async def get_jwks_endpoint():
    """Get JSON Web Key Set for validating internal tokens.

    This endpoint serves the public key used to verify internal JWTs.
    Clients can use this to validate tokens without calling the API.
    """
    return get_jwks()


@router.get("/auth/config", response_model=AuthConfigResponse)
async def get_auth_config():
    """Get authentication configuration for SDK/CLI and UI clients.

    Allows clients to dynamically discover how to authenticate against this
    registry (auth mode, OIDC provider details) instead of requiring
    build-time configuration.
    """
    if auth_settings.mode == "local":
        return AuthConfigResponse(
            auth_mode="local",
            local_registration_enabled=auth_settings.local_registration_enabled,
        )
    return AuthConfigResponse(
        auth_mode="oidc",
        oidc_issuer=oidc_settings.client_issuer_url,
        oidc_client_id=oidc_settings.sdk_client_id,
        oidc_ui_client_id=oidc_settings.ui_client_id,
        cognito_domain=oidc_settings.cognito_domain,
    )


async def get_exchange_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(oidc_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the user for token exchange from the bearer credential.

    Accepts either a session token (local auth mode only) or an OIDC token.
    Session tokens are tried first: they are minted by this API, so
    validation is local and cheap. They are only honored while local mode
    is active, so tokens minted before a deployment switched to OIDC can't
    keep authenticating until expiry.
    """
    token_str = credentials.credentials

    session_payload = None
    if auth_settings.mode == "local":
        token_manager = get_token_manager()
        try:
            session_payload = token_manager.validate_session_token(token_str)
        except TokenError:
            session_payload = None

    if session_payload is not None:
        result = await db.execute(
            select(User).where(User.id == UUID(session_payload.sub))
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        if session_invalidated_by_password_change(user, session_payload.iat):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session is no longer valid, please log in again",
            )
        return user

    # Fall back to OIDC token
    validator = get_jwt_validator()
    try:
        oidc_token = await validator.validate_token(token_str)
    except AuthenticationError as e:
        logger.warning("Token exchange: token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    return await get_or_create_user(db, oidc_token)


@router.post("/auth/exchange", response_model=TokenExchangeResponse)
async def exchange_token(
    request: TokenExchangeRequest,
    user: Annotated[User, Depends(get_exchange_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Exchange an OIDC or session token for a workspace-scoped internal token.

    This is the only endpoint that accepts OIDC/session tokens directly
    (besides the bootstrap endpoints). All other endpoints require internal
    tokens from this exchange.

    Args:
        request: Contains the workspace_id to scope the token to
        user: User resolved from the bearer credential (OIDC or session token)
        db: Database session

    Returns:
        Internal access token scoped to the requested workspace

    Raises:
        401: Invalid token
        403: User is not a member of the requested workspace
        404: Workspace not found
    """

    # Verify user is a member of the requested workspace
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == request.workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    membership = result.scalar_one_or_none()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of the requested workspace",
        )

    # Create internal token with workspace_id
    token_manager = get_token_manager()
    access_token = token_manager.create_access_token(
        user_id=str(user.id),
        workspace_id=str(request.workspace_id),
    )

    # Calculate expires_in from TTL
    expires_in = int(token_manager.access_token_ttl.total_seconds())

    return TokenExchangeResponse(
        access_token=access_token,
        expires_in=expires_in,
    )


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting (proxy-aware).

    Uses the LAST X-Forwarded-For entry: proxies append the peer address
    they saw, so the last entry was added by the nearest (trusted) hop,
    while earlier entries are client-supplied and trivially spoofable.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else "unknown"


def _require_local_mode() -> None:
    if auth_settings.mode != "local":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Local authentication is not enabled on this server",
        )


def _mint_session_response(user: User) -> SessionTokenResponse:
    token_manager = get_token_manager()
    ttl = timedelta(hours=auth_settings.session_token_ttl_hours)
    session_token = token_manager.create_session_token(str(user.id), ttl)
    return SessionTokenResponse(
        session_token=session_token,
        expires_in=int(ttl.total_seconds()),
    )


@router.post("/auth/login", response_model=SessionTokenResponse)
async def local_login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Authenticate with email/password (local auth mode only).

    Returns a user-scoped session token to exchange for workspace tokens
    via /auth/exchange (and accepted by bootstrap endpoints like /ui/me).

    Raises:
        403: Local auth mode not enabled
        401: Invalid credentials
        429: Too many attempts
    """
    _require_local_mode()

    # Two rate-limit layers: per email+IP, plus per email only so rotating
    # (spoofable) client IPs alone can't brute-force a single account.
    email_key = body.email.lower()
    ip_key = f"{email_key}|{_client_ip(request)}"
    if not login_rate_limiter.check(ip_key) or not login_email_rate_limiter.check(
        email_key
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts, try again later",
        )

    user = await authenticate_local_user(db, body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    login_rate_limiter.reset(ip_key)
    login_email_rate_limiter.reset(email_key)
    logger.info("Local login: %s", user.id)
    return _mint_session_response(user)


@router.post("/auth/register", response_model=SessionTokenResponse)
async def local_register(
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Self-service registration (local auth mode, when enabled).

    Creates the user with a personal workspace and returns a session token
    (auto-login).

    Raises:
        403: Local auth mode or registration not enabled
        400: Password policy violation
        409: Email already registered
    """
    _require_local_mode()
    if not auth_settings.local_registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is not enabled on this server",
        )

    try:
        user = await create_local_user(
            db,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
        )
    except PasswordPolicyError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return _mint_session_response(user)


@router.post("/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def local_change_password(
    body: ChangePasswordRequest,
    user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Change the current user's password (local auth mode only).

    Raises:
        403: Local auth mode not enabled, or user has no password (OIDC user)
        401: Current password incorrect
        400: New password fails policy
    """
    _require_local_mode()
    if user.password_hash is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User has no password set",
        )
    if not await verify_password_async(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    try:
        validate_password_policy(body.new_password)
    except PasswordPolicyError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    user.password_hash = await hash_password_async(body.new_password)
    # Invalidate outstanding session tokens: session tokens with iat before
    # this instant are rejected (see session_invalidated_by_password_change).
    user.password_changed_at = utc_now()
    await db.commit()
    logger.info("Password changed for user %s", user.id)
