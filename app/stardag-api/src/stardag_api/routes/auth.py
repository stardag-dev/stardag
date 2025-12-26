"""Authentication routes for token exchange and JWKS."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.jwt import (
    AuthenticationError,
    TokenPayload,
    get_jwt_validator,
)
from stardag_api.auth.tokens import (
    get_jwks,
    get_token_manager,
)
from stardag_api.db import get_db
from stardag_api.models import OrganizationMember, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# Bearer scheme for Keycloak tokens (only used by /auth/exchange)
keycloak_bearer = HTTPBearer(auto_error=True)


class TokenExchangeRequest(BaseModel):
    """Request body for token exchange."""

    org_id: str


class TokenExchangeResponse(BaseModel):
    """Response from token exchange."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds


async def get_keycloak_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(keycloak_bearer)],
) -> TokenPayload:
    """Validate Keycloak JWT and return payload.

    This is only used by the /auth/exchange endpoint.
    All other endpoints use internal tokens.
    """
    validator = get_jwt_validator()
    try:
        return await validator.validate_token(credentials.credentials)
    except AuthenticationError as e:
        logger.warning("Keycloak token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_or_create_user(db: AsyncSession, token: TokenPayload) -> User:
    """Get existing user or create new one from Keycloak token claims."""
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
    logger.info("Created new user %s from Keycloak token", user.id)
    return user


@router.get("/.well-known/jwks.json")
async def get_jwks_endpoint():
    """Get JSON Web Key Set for validating internal tokens.

    This endpoint serves the public key used to verify internal JWTs.
    Clients can use this to validate tokens without calling the API.
    """
    return get_jwks()


@router.post("/auth/exchange", response_model=TokenExchangeResponse)
async def exchange_token(
    request: TokenExchangeRequest,
    keycloak_token: Annotated[TokenPayload, Depends(get_keycloak_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Exchange a Keycloak token for an org-scoped internal token.

    This is the only endpoint that accepts Keycloak tokens.
    All other endpoints require internal tokens from this exchange.

    Args:
        request: Contains the org_id to scope the token to
        keycloak_token: Validated Keycloak JWT (from Authorization header)
        db: Database session

    Returns:
        Internal access token scoped to the requested organization

    Raises:
        401: Invalid Keycloak token
        403: User is not a member of the requested organization
        404: Organization not found
    """
    # Get or create user from Keycloak token
    user = await get_or_create_user(db, keycloak_token)

    # Verify user is a member of the requested organization
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == request.org_id,
            OrganizationMember.user_id == user.id,
        )
    )
    membership = result.scalar_one_or_none()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of the requested organization",
        )

    # Create internal token with org_id
    token_manager = get_token_manager()
    access_token = token_manager.create_access_token(
        user_id=user.id,
        org_id=request.org_id,
    )

    # Calculate expires_in from TTL
    expires_in = int(token_manager.access_token_ttl.total_seconds())

    return TokenExchangeResponse(
        access_token=access_token,
        expires_in=expires_in,
    )
