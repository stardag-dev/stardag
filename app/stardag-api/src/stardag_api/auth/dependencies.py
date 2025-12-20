"""FastAPI dependencies for authentication."""

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.jwt import (
    AuthenticationError,
    TokenPayload,
    get_jwt_validator,
)
from stardag_api.db import get_db
from stardag_api.models import User

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
    await db.commit()
    await db.refresh(user)
    logger.info("Created new user %s from OIDC token", user.id)

    return user


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
