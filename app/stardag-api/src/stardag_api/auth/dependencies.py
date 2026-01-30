"""FastAPI dependencies for authentication.

This module provides authentication dependencies for the Stardag API.

Token types:
- Internal tokens: Workspace-scoped JWTs minted by /auth/exchange. Required for most
  endpoints.
- OIDC tokens: External JWTs from the OIDC provider. Accepted by:
  - /auth/exchange (to mint internal tokens)
  - /ui/me, /ui/me/invites (bootstrap endpoints before workspace selection)
- API keys: Environment-scoped keys for SDK/automation. Alternative to JWTs.
"""

import logging
import re
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
    TargetRoot,
)
from stardag_api.services import api_keys as api_key_service

logger = logging.getLogger(__name__)


async def create_personal_workspace_for_user(db: AsyncSession, user: User) -> Workspace:
    """Create a personal workspace for a new user.

    Creates a workspace with:
    - is_personal=True
    - A "local" environment
    - A default target root pointing to ~/.stardag/local-target-roots/default
    """
    # Generate slug from email prefix
    email_prefix = user.email.split("@")[0].lower()
    base_slug = re.sub(r"[^a-z0-9]+", "-", email_prefix).strip("-")[:50]

    # Ensure uniqueness with suffix
    slug = base_slug
    suffix = 0
    while True:
        existing = await db.execute(select(Workspace).where(Workspace.slug == slug))
        if not existing.scalar_one_or_none():
            break
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    # Create workspace
    workspace = Workspace(
        name=f"{user.display_name or email_prefix}'s Workspace",
        slug=slug,
        is_personal=True,
        created_by_id=user.id,
    )
    db.add(workspace)
    await db.flush()

    # Add user as owner
    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.OWNER,
    )
    db.add(membership)

    # Create "local" environment (not "default")
    environment = Environment(
        workspace_id=workspace.id,
        name="Local",
        slug="local",
        description="Local development environment",
    )
    db.add(environment)
    await db.flush()

    # Create default target root pointing to ~/.stardag/local-target-roots/default
    target_root = TargetRoot(
        environment_id=environment.id,
        name="default",
        uri_prefix="~/.stardag/local-target-roots/default",
    )
    db.add(target_root)

    logger.info(
        "Created personal workspace %s for user %s",
        workspace.slug,
        user.id,
    )

    return workspace


async def get_or_create_user_from_oidc_claims(
    db: AsyncSession,
    external_id: str,
    email: str,
    display_name: str | None,
) -> User:
    """Get or create a user from OIDC claims, handling race conditions.

    This function handles the case where multiple requests try to create the same
    user simultaneously by catching IntegrityError and retrying the lookup.
    """
    # Look up user by external_id (OIDC subject claim)
    result = await db.execute(select(User).where(User.external_id == external_id))
    user = result.scalar_one_or_none()

    if user is not None:
        # Update user info if changed
        updated = False
        if email and user.email != email:
            user.email = email
            updated = True
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            updated = True
        if updated:
            await db.commit()
            logger.info("Updated user %s with new info from OIDC token", user.id)
        return user

    # User not found by external_id - check if they exist by email
    # This handles identity provider changes (same email, different subject)
    if email:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is not None:
            # Found user by email - update external_id to new provider
            logger.info(
                "User %s changed identity provider (old external_id: %s, new: %s)",
                user.id,
                user.external_id,
                external_id,
            )
            user.external_id = external_id
            if display_name and user.display_name != display_name:
                user.display_name = display_name
            await db.commit()
            return user

    # Create new user
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token must contain email claim for user creation",
        )

    try:
        user = User(
            external_id=external_id,
            email=email,
            display_name=display_name,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
        logger.info("Created new user %s from OIDC token", user.id)

        # Auto-create personal workspace for new user
        await create_personal_workspace_for_user(db, user)
        await db.commit()

        return user
    except IntegrityError:
        # Race condition: another request created the user
        # Rollback and retry the lookup
        await db.rollback()
        logger.info("Race condition on user creation, retrying lookup for %s", email)

        # Retry lookup by external_id first, then by email
        result = await db.execute(select(User).where(User.external_id == external_id))
        user = result.scalar_one_or_none()
        if user is not None:
            return user

        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is not None:
            # Update external_id if needed
            if user.external_id != external_id:
                user.external_id = external_id
                await db.commit()
            return user

        # This shouldn't happen, but raise an error if we can't find the user
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create or find user after race condition",
        )


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


async def get_user_by_id(db: AsyncSession, user_id: str | UUID) -> User | None:
    """Get a user by internal ID."""
    if isinstance(user_id, str):
        user_id = UUID(user_id)
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


def get_workspace_id_from_token(
    token: Annotated[InternalTokenPayload, Depends(get_token)],
) -> str:
    """Get the workspace ID from the internal token.

    Internal tokens always have workspace_id - it's required.
    """
    return token.workspace_id


async def verify_environment_access(
    db: AsyncSession,
    environment_id: str | UUID,
    token_workspace_id: str | UUID,
) -> Environment:
    """Verify environment exists and belongs to the token's workspace.

    Args:
        db: Database session
        environment_id: Environment to verify
        token_workspace_id: Workspace ID from the token (must match environment's workspace)

    Returns:
        The environment if valid

    Raises:
        HTTPException: 404 if environment not found or invalid UUID, 403 if workspace mismatch
    """
    # Convert strings to UUID if needed
    try:
        if isinstance(environment_id, str):
            environment_id = UUID(environment_id)
        if isinstance(token_workspace_id, str):
            token_workspace_id = UUID(token_workspace_id)
    except ValueError:
        # Invalid UUID string - treat as not found
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Environment not found",
        )

    # Get the environment
    environment = await db.get(Environment, environment_id)
    if not environment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Environment not found",
        )

    # Verify environment belongs to the token's workspace
    if environment.workspace_id != token_workspace_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Environment does not belong to your workspace",
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
    workspace_id: UUID  # Workspace ID (from token or API key's environment)
    api_key: ApiKey | None = None
    user: User | None = None

    @property
    def environment_id(self) -> UUID:
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
            workspace_id=api_key_auth.environment.workspace_id,
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

        # Verify environment belongs to the token's workspace
        environment = await verify_environment_access(
            db, environment_id, token.workspace_id
        )

        return SdkAuth(
            environment=environment,
            workspace_id=UUID(token.workspace_id),
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
    selected a workspace (e.g., /ui/me, /ui/me/invites).

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
    return await get_or_create_user_from_oidc_claims(
        db=db,
        external_id=oidc_token.sub,
        email=oidc_token.email or "",
        display_name=oidc_token.display_name,
    )


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
    return await get_or_create_user_from_oidc_claims(
        db=db,
        external_id=oidc_payload.sub,
        email=oidc_payload.email or "",
        display_name=oidc_payload.display_name,
    )
