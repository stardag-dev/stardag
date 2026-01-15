"""FastAPI dependencies for authentication.

This module provides authentication dependencies for the Stardag API.

Token types:
- Internal tokens: Org-scoped JWTs minted by /auth/exchange. Required for most
  endpoints.
- OIDC tokens: External JWTs from the OIDC provider. Accepted by:
  - /auth/exchange (to mint internal tokens)
  - /ui/me, /ui/me/invites (bootstrap endpoints before org selection)
- API keys: Environment-scoped keys for SDK/automation. Alternative to JWTs.
"""

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.jwt import (
    AuthenticationError,
    TokenPayload as OIDCTokenPayload,
    get_jwt_validator,
)
from stardag_api.auth.tokens import (
    InternalTokenPayload,
    TokenExpiredError,
    TokenInvalidError,
    get_token_manager,
)
from stardag_api.db import get_db
from stardag_api.models import (
    ApiKey,
    User,
    Environment,
)
from stardag_api.services import api_keys as api_key_service

logger = logging.getLogger(__name__)

# HTTP Bearer token security scheme
# auto_error=False allows us to handle missing tokens gracefully
bearer_scheme = HTTPBearer(auto_error=False)


async def get_optional_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> InternalTokenPayload | None:
    """Get and validate internal JWT token if present.

    Returns None if no token provided, raises HTTPException if token is invalid.

    NOTE: This validates INTERNAL tokens only (from /auth/exchange).
    OIDC tokens are NOT accepted here.
    """
    if credentials is None:
        return None

    token_manager = get_token_manager()
    try:
        return token_manager.validate_token(credentials.credentials)
    except TokenExpiredError as e:
        logger.warning("Token expired: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except TokenInvalidError as e:
        logger.warning("Token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> InternalTokenPayload:
    """Get and validate internal JWT token (required).

    Raises HTTPException if no token provided or token is invalid.

    NOTE: This validates INTERNAL tokens only (from /auth/exchange).
    OIDC tokens are NOT accepted here.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_manager = get_token_manager()
    try:
        return token_manager.validate_token(credentials.credentials)
    except TokenExpiredError as e:
        logger.warning("Token expired: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except TokenInvalidError as e:
        logger.warning("Token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    """Get a user by internal ID."""
    return await db.get(User, user_id)


async def get_current_user(
    token: Annotated[InternalTokenPayload, Depends(get_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Get the current authenticated user (required).

    Internal tokens contain the user's internal ID in the 'sub' claim.
    Raises HTTPException if not authenticated or user not found.
    """
    user = await get_user_by_id(db, token.sub)
    if user is None:
        # This shouldn't happen - token was issued for a valid user
        logger.error("User %s from token not found in database", token.sub)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


async def get_current_user_optional(
    token: Annotated[InternalTokenPayload | None, Depends(get_optional_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Get the current user if authenticated, None otherwise."""
    if token is None:
        return None
    user = await get_user_by_id(db, token.sub)
    if user is None:
        logger.error("User %s from token not found in database", token.sub)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


def get_org_id_from_token(
    token: Annotated[InternalTokenPayload, Depends(get_token)],
) -> str:
    """Get the organization ID from the internal token.

    Internal tokens always have org_id - it's required.
    """
    return token.org_id


async def verify_environment_access(
    db: AsyncSession,
    environment_id: str,
    token_org_id: str,
) -> Environment:
    """Verify environment exists and belongs to the token's organization.

    Args:
        db: Database session
        environment_id: Environment to verify
        token_org_id: Organization ID from the token (must match environment's org)

    Returns:
        The environment if valid

    Raises:
        HTTPException: 404 if environment not found, 403 if org mismatch
    """
    # Get the environment
    environment = await db.get(Environment, environment_id)
    if not environment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Environment not found",
        )

    # Verify environment belongs to the token's organization
    if environment.organization_id != token_org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Environment does not belong to your organization",
        )

    return environment


# --- API Key Authentication ---


@dataclass
class ApiKeyAuth:
    """Authentication context from an API key."""

    api_key: ApiKey
    environment: Environment


async def get_api_key_auth(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiKeyAuth | None:
    """Get and validate API key if present.

    Returns ApiKeyAuth if a valid API key is provided, None otherwise.
    Raises HTTPException if an API key is provided but invalid.
    """
    if x_api_key is None:
        return None

    # Validate the API key
    api_key = await api_key_service.validate_api_key(db, x_api_key)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Get the environment for the API key
    environment = await db.get(Environment, api_key.environment_id)
    if environment is None:
        # This shouldn't happen, but handle it gracefully
        logger.error(
            "API key %s has invalid environment_id %s",
            api_key.id,
            api_key.environment_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key configuration error",
        )

    return ApiKeyAuth(api_key=api_key, environment=environment)


async def require_api_key_auth(
    api_key_auth: Annotated[ApiKeyAuth | None, Depends(get_api_key_auth)],
) -> ApiKeyAuth:
    """Require API key authentication.

    Raises HTTPException if no API key is provided.
    """
    if api_key_auth is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return api_key_auth


@dataclass
class SdkAuth:
    """Authentication context for SDK endpoints.

    Supports either API key or internal JWT authentication.
    """

    environment: Environment
    org_id: str  # Organization ID (from token or API key's environment)
    api_key: ApiKey | None = None
    user: User | None = None

    @property
    def environment_id(self) -> str:
        """Get the environment ID for convenience."""
        return self.environment.id


async def get_sdk_auth(
    db: Annotated[AsyncSession, Depends(get_db)],
    api_key_auth: Annotated[ApiKeyAuth | None, Depends(get_api_key_auth)],
    token: Annotated[InternalTokenPayload | None, Depends(get_optional_token)],
    environment_id: str | None = None,
) -> SdkAuth | None:
    """Get SDK authentication from either API key or internal JWT token.

    Priority:
    1. API key (if X-API-Key header present)
    2. Internal JWT token + environment_id parameter

    Returns SdkAuth if authenticated, None if no authentication provided.
    Raises HTTPException if authentication is invalid.
    """
    # API key takes priority
    if api_key_auth is not None:
        return SdkAuth(
            environment=api_key_auth.environment,
            org_id=api_key_auth.environment.organization_id,
            api_key=api_key_auth.api_key,
        )

    # Fall back to internal JWT token
    if token is not None:
        user = await get_user_by_id(db, token.sub)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )

        # JWT requires environment_id parameter
        if environment_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="environment_id is required when using JWT authentication",
            )

        # Verify environment belongs to the token's organization
        environment = await verify_environment_access(db, environment_id, token.org_id)

        return SdkAuth(
            environment=environment,
            org_id=token.org_id,
            user=user,
        )

    return None


async def require_sdk_auth(
    sdk_auth: Annotated[SdkAuth | None, Depends(get_sdk_auth)],
) -> SdkAuth:
    """Require SDK authentication (either API key or JWT).

    Raises HTTPException if no authentication is provided.
    """
    if sdk_auth is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide X-API-Key header or Bearer token.",
            headers={"WWW-Authenticate": 'Bearer, ApiKey realm="sdk"'},
        )
    return sdk_auth


# --- OIDC Token Authentication (for bootstrap endpoints) ---


async def get_oidc_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> OIDCTokenPayload:
    """Get and validate OIDC JWT token (required).

    Used for bootstrap endpoints that need to work before the user has
    selected an organization (e.g., /ui/me, /ui/me/invites).

    Raises HTTPException if no token provided or token is invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

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


async def get_or_create_user_from_oidc(
    oidc_token: Annotated[OIDCTokenPayload, Depends(get_oidc_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Get or create user from OIDC token claims.

    Used for bootstrap endpoints that need user info before org selection.
    Creates user if they don't exist yet (first login).

    If the user changed identity providers (same email, different external_id),
    we update the external_id to match the current provider.
    """
    # Look up user by external_id (OIDC subject claim)
    result = await db.execute(select(User).where(User.external_id == oidc_token.sub))
    user = result.scalar_one_or_none()

    if user is not None:
        # Update user info if changed
        updated = False
        if oidc_token.email and user.email != oidc_token.email:
            user.email = oidc_token.email
            updated = True
        if oidc_token.display_name and user.display_name != oidc_token.display_name:
            user.display_name = oidc_token.display_name
            updated = True
        if updated:
            await db.commit()
            logger.info("Updated user %s with new info from OIDC token", user.id)
        return user

    # User not found by external_id - check if they exist by email
    # This handles identity provider changes (same email, different subject)
    if oidc_token.email:
        result = await db.execute(select(User).where(User.email == oidc_token.email))
        user = result.scalar_one_or_none()

        if user is not None:
            # Found user by email - update external_id to new provider
            logger.info(
                "User %s changed identity provider (old external_id: %s, new: %s)",
                user.id,
                user.external_id,
                oidc_token.sub,
            )
            user.external_id = oidc_token.sub
            if oidc_token.display_name and user.display_name != oidc_token.display_name:
                user.display_name = oidc_token.display_name
            await db.commit()
            return user

    # Create new user
    if not oidc_token.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token must contain email claim for user creation",
        )

    user = User(
        external_id=oidc_token.sub,
        email=oidc_token.email,
        display_name=oidc_token.display_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Created new user %s from OIDC token", user.id)
    return user


async def get_current_user_flexible(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Get the current user from either internal token or OIDC token.

    This is for endpoints that need to work in both scenarios:
    - New users with only an OIDC token (first login, no org yet)
    - Existing users with an org-scoped internal token

    Priority: Internal token first (most common for logged-in users), then OIDC.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_str = credentials.credentials

    # Try internal token first (most common case for logged-in users)
    token_manager = get_token_manager()
    try:
        internal_payload = token_manager.validate_token(token_str)
        # Got a valid internal token - look up user by internal ID
        user = await get_user_by_id(db, internal_payload.sub)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        return user
    except (TokenExpiredError, TokenInvalidError):
        # Not a valid internal token, try OIDC
        pass

    # Try OIDC token
    validator = get_jwt_validator()
    try:
        oidc_payload = await validator.validate_token(token_str)
    except AuthenticationError as e:
        logger.warning("Both internal and OIDC token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    # Got a valid OIDC token - get or create user
    result = await db.execute(select(User).where(User.external_id == oidc_payload.sub))
    user = result.scalar_one_or_none()

    if user is not None:
        # Update user info if changed
        updated = False
        if oidc_payload.email and user.email != oidc_payload.email:
            user.email = oidc_payload.email
            updated = True
        if oidc_payload.display_name and user.display_name != oidc_payload.display_name:
            user.display_name = oidc_payload.display_name
            updated = True
        if updated:
            await db.commit()
            logger.info("Updated user %s with new info from OIDC token", user.id)
        return user

    # User not found by external_id - check if they exist by email
    # This handles identity provider changes (same email, different subject)
    if oidc_payload.email:
        result = await db.execute(select(User).where(User.email == oidc_payload.email))
        user = result.scalar_one_or_none()

        if user is not None:
            # Found user by email - update external_id to new provider
            logger.info(
                "User %s changed identity provider (old external_id: %s, new: %s)",
                user.id,
                user.external_id,
                oidc_payload.sub,
            )
            user.external_id = oidc_payload.sub
            if (
                oidc_payload.display_name
                and user.display_name != oidc_payload.display_name
            ):
                user.display_name = oidc_payload.display_name
            await db.commit()
            return user

    # Create new user from OIDC token
    if not oidc_payload.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token must contain email claim for user creation",
        )

    user = User(
        external_id=oidc_payload.sub,
        email=oidc_payload.email,
        display_name=oidc_payload.display_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Created new user %s from OIDC token", user.id)
    return user
