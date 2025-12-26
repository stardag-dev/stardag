"""UI-specific routes for bootstrap (Keycloak tokens) and authenticated operations.

Bootstrap endpoints (/me, /me/invites) accept Keycloak tokens directly because
they are needed before the user can select an organization for token exchange.

Other UI endpoints require org-scoped internal tokens.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import get_or_create_user_from_keycloak
from stardag_api.db import get_db
from stardag_api.models import (
    Invite,
    InviteStatus,
    Organization,
    OrganizationMember,
    OrganizationRole,
    User,
)

router = APIRouter(prefix="/ui", tags=["ui"])


# --- Schemas ---


class UserProfileResponse(BaseModel):
    """Current user profile."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    external_id: str
    email: str
    display_name: str | None


class OrganizationSummary(BaseModel):
    """Summary of an organization for listing."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    role: OrganizationRole


class UserProfileWithOrgsResponse(BaseModel):
    """User profile with their organizations."""

    user: UserProfileResponse
    organizations: list[OrganizationSummary]


class PendingInviteResponse(BaseModel):
    """Pending invite for current user."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    organization_name: str
    role: OrganizationRole
    invited_by_email: str | None


# --- Bootstrap Endpoints (accept Keycloak tokens) ---


@router.get("/me", response_model=UserProfileWithOrgsResponse)
async def get_current_user_profile(
    current_user: Annotated[User, Depends(get_or_create_user_from_keycloak)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the current user's profile and organizations.

    This is a bootstrap endpoint that accepts Keycloak tokens directly.
    It returns the user's organizations so they can select one for token exchange.
    """
    # Get user's organization memberships
    result = await db.execute(
        select(OrganizationMember, Organization)
        .join(Organization, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == current_user.id)
    )
    memberships = result.all()

    organizations = [
        OrganizationSummary(
            id=org.id,
            name=org.name,
            slug=org.slug,
            role=membership.role,
        )
        for membership, org in memberships
    ]

    return UserProfileWithOrgsResponse(
        user=UserProfileResponse(
            id=current_user.id,
            external_id=current_user.external_id,
            email=current_user.email,
            display_name=current_user.display_name,
        ),
        organizations=organizations,
    )


@router.get("/me/invites", response_model=list[PendingInviteResponse])
async def get_pending_invites(
    current_user: Annotated[User, Depends(get_or_create_user_from_keycloak)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get pending invites for the current user.

    This is a bootstrap endpoint that accepts Keycloak tokens directly.
    """
    result = await db.execute(
        select(Invite, Organization, User)
        .join(Organization, Invite.organization_id == Organization.id)
        .outerjoin(User, Invite.invited_by_id == User.id)
        .where(
            Invite.email == current_user.email,
            Invite.status == InviteStatus.PENDING,
        )
    )
    invites = result.all()

    return [
        PendingInviteResponse(
            id=invite.id,
            organization_id=invite.organization_id,
            organization_name=org.name,
            role=invite.role,
            invited_by_email=invited_by.email if invited_by else None,
        )
        for invite, org, invited_by in invites
    ]
