"""FastAPI dependencies for authentication."""

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.jwt import (
    AuthenticationError,
    TokenPayload,
    get_jwt_validator,
)
from stardag_api.db import get_db
from stardag_api.models import (
    ApiKey,
    Invite,
    Organization,
    OrganizationMember,
    User,
    Workspace,
)
from stardag_api.models.enums import InviteStatus, OrganizationRole
from stardag_api.services import api_keys as api_key_service

logger = logging.getLogger(__name__)

# HTTP Bearer token security scheme
# auto_error=False allows us to handle missing tokens gracefully
bearer_scheme = HTTPBearer(auto_error=False)


async def get_optional_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TokenPayload | None:
    """Get and validate JWT token if present.

    Returns None if no token provided, raises HTTPException if token is invalid.
    """
    if credentials is None:
        return None

    validator = get_jwt_validator()
    try:
        return await validator.validate_token(credentials.credentials)
    except AuthenticationError as e:
        logger.warning("Token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TokenPayload:
    """Get and validate JWT token (required).

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
        logger.warning("Token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_or_create_user(
    db: AsyncSession,
    token: TokenPayload,
) -> User:
    """Get existing user or create new one from token claims.

    This implements user auto-provisioning on first login.
    """
    # Look up user by external_id (OIDC subject claim)
    result = await db.execute(select(User).where(User.external_id == token.sub))
    user = result.scalar_one_or_none()

    if user is not None:
        # Update user info if changed (email, name)
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

    # Create new user from token claims
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
    await db.flush()  # Get user.id without committing
    logger.info("Created new user %s from OIDC token", user.id)

    # Check if user has pending invites
    pending_invites_result = await db.execute(
        select(Invite).where(
            Invite.email == token.email,
            Invite.status == InviteStatus.PENDING,
        )
    )
    pending_invites = pending_invites_result.scalars().all()

    if pending_invites:
        # User has pending invites - don't create personal org
        # They should accept invites to join existing orgs
        logger.info(
            "User %s has %d pending invite(s), skipping personal org creation",
            user.id,
            len(pending_invites),
        )
        await db.commit()
        await db.refresh(user)
        return user

    # No pending invites - create a personal organization for the new user
    username = token.email.split("@")[0]
    org_name = f"{username}'s Organization"
    org_slug = _generate_unique_slug(username)

    org = Organization(
        name=org_name,
        slug=org_slug,
        description="Personal organization",
        created_by_id=user.id,
    )
    db.add(org)
    await db.flush()  # Get org.id

    # Add user as owner of the organization
    membership = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )
    db.add(membership)

    # Create default workspace
    workspace = Workspace(
        organization_id=org.id,
        name="Default",
        slug="default",
        description="Default workspace",
    )
    db.add(workspace)

    await db.commit()
    await db.refresh(user)
    logger.info(
        "Created personal organization %s and workspace for user %s",
        org.id,
        user.id,
    )

    return user


def _generate_unique_slug(base: str) -> str:
    """Generate a URL-safe slug from a base string."""
    import re
    import uuid

    # Convert to lowercase, replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    # Add a short unique suffix to avoid collisions
    suffix = uuid.uuid4().hex[:6]
    return f"{slug}-{suffix}"


async def get_current_user(
    token: Annotated[TokenPayload, Depends(get_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Get the current authenticated user (required).

    Creates user on first login if they don't exist.
    Raises HTTPException if not authenticated.
    """
    return await get_or_create_user(db, token)


async def get_current_user_optional(
    token: Annotated[TokenPayload | None, Depends(get_optional_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Get the current user if authenticated, None otherwise.

    Creates user on first login if they don't exist.
    """
    if token is None:
        return None
    return await get_or_create_user(db, token)


async def verify_workspace_access(
    db: AsyncSession,
    user: User,
    workspace_id: str,
) -> Workspace:
    """Verify user has access to a workspace.

    Returns the workspace if access is granted, raises HTTPException otherwise.
    """
    # Get the workspace
    workspace = await db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )

    # Check if user is a member of the workspace's organization
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == workspace.organization_id,
            OrganizationMember.user_id == user.id,
        )
    )
    membership = result.scalar_one_or_none()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this workspace",
        )

    return workspace


# --- API Key Authentication ---


@dataclass
class ApiKeyAuth:
    """Authentication context from an API key."""

    api_key: ApiKey
    workspace: Workspace


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

    # Get the workspace for the API key
    workspace = await db.get(Workspace, api_key.workspace_id)
    if workspace is None:
        # This shouldn't happen, but handle it gracefully
        logger.error(
            "API key %s has invalid workspace_id %s", api_key.id, api_key.workspace_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key configuration error",
        )

    return ApiKeyAuth(api_key=api_key, workspace=workspace)


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

    Supports either API key or JWT authentication.
    """

    workspace: Workspace
    api_key: ApiKey | None = None
    user: User | None = None

    @property
    def workspace_id(self) -> str:
        """Get the workspace ID for convenience."""
        return self.workspace.id


async def get_sdk_auth(
    db: Annotated[AsyncSession, Depends(get_db)],
    api_key_auth: Annotated[ApiKeyAuth | None, Depends(get_api_key_auth)],
    token: Annotated[TokenPayload | None, Depends(get_optional_token)],
    workspace_id: str | None = None,
) -> SdkAuth | None:
    """Get SDK authentication from either API key or JWT token.

    Priority:
    1. API key (if X-API-Key header present)
    2. JWT token + workspace_id parameter

    Returns SdkAuth if authenticated, None if no authentication provided.
    Raises HTTPException if authentication is invalid.
    """
    # API key takes priority
    if api_key_auth is not None:
        return SdkAuth(
            workspace=api_key_auth.workspace,
            api_key=api_key_auth.api_key,
        )

    # Fall back to JWT token
    if token is not None:
        user = await get_or_create_user(db, token)

        # JWT requires workspace_id parameter
        if workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_id is required when using JWT authentication",
            )

        # Verify user has access to workspace
        workspace = await verify_workspace_access(db, user, workspace_id)

        return SdkAuth(
            workspace=workspace,
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
