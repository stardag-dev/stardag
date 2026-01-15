"""Organization management routes (UI, requires auth).

The create organization endpoint accepts OIDC tokens (bootstrap endpoint)
since users need to create an org before they can do token exchange.

Other endpoints require org-scoped internal tokens.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from stardag_api.auth import get_current_user, get_current_user_flexible
from stardag_api.db import get_db
from stardag_api.models import (
    Invite,
    InviteStatus,
    Organization,
    OrganizationMember,
    OrganizationRole,
    TargetRoot,
    User,
    Environment,
)
from stardag_api.services import api_keys as api_key_service

router = APIRouter(prefix="/ui/organizations", tags=["organizations"])


# --- Schemas ---


class OrganizationCreate(BaseModel):
    """Create a new organization."""

    name: str
    slug: str
    description: str | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError(
                "Slug must be lowercase alphanumeric with hyphens, "
                "cannot start or end with hyphen"
            )
        if len(v) < 2 or len(v) > 64:
            raise ValueError("Slug must be between 2 and 64 characters")
        return v


class OrganizationUpdate(BaseModel):
    """Update an organization."""

    name: str | None = None
    description: str | None = None


class OrganizationResponse(BaseModel):
    """Organization response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    description: str | None


class OrganizationDetailResponse(OrganizationResponse):
    """Organization with member count."""

    member_count: int
    environment_count: int


class MemberResponse(BaseModel):
    """Organization member."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    email: str
    display_name: str | None
    role: OrganizationRole


class MemberUpdateRole(BaseModel):
    """Update member role."""

    role: OrganizationRole


class InviteCreate(BaseModel):
    """Create an invite."""

    email: str
    role: OrganizationRole = OrganizationRole.MEMBER


class InviteResponse(BaseModel):
    """Invite response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: OrganizationRole
    status: InviteStatus
    invited_by_email: str | None


class EnvironmentCreate(BaseModel):
    """Create an environment."""

    name: str
    slug: str
    description: str | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError(
                "Slug must be lowercase alphanumeric with hyphens, "
                "cannot start or end with hyphen"
            )
        if len(v) < 2 or len(v) > 64:
            raise ValueError("Slug must be between 2 and 64 characters")
        return v


class EnvironmentUpdate(BaseModel):
    """Update an environment."""

    name: str | None = None
    description: str | None = None


class EnvironmentResponse(BaseModel):
    """Environment response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    slug: str
    description: str | None
    owner_id: str | None = None  # Non-null for personal environments


# --- Helper functions ---


def generate_personal_environment_slug(email: str, suffix: int = 0) -> str:
    """Generate a personal environment slug from email.

    Format: personal-{email_prefix} or personal-{email_prefix}-{suffix}
    """
    email_prefix = email.split("@")[0].lower()
    # Clean up for slug: only alphanumeric and hyphens
    clean_prefix = re.sub(r"[^a-z0-9]+", "-", email_prefix).strip("-")[:40]
    if suffix > 0:
        return f"personal-{clean_prefix}-{suffix}"
    return f"personal-{clean_prefix}"


async def create_personal_environment(
    db: AsyncSession, org_id: str, user: User
) -> Environment:
    """Create a personal environment for a user in an organization.

    Ensures unique slug by appending numeric suffix if needed.
    """
    base_slug = generate_personal_environment_slug(user.email)

    # Check for existing slugs and find unique one
    suffix = 0
    slug = base_slug
    while True:
        existing = await db.execute(
            select(Environment).where(
                Environment.organization_id == org_id,
                Environment.slug == slug,
            )
        )
        if not existing.scalar_one_or_none():
            break
        suffix += 1
        slug = generate_personal_environment_slug(user.email, suffix)

    environment = Environment(
        organization_id=org_id,
        name=f"Personal ({user.email.split('@')[0]})",
        slug=slug,
        description=f"Personal environment for {user.email}",
        owner_id=user.id,
    )
    db.add(environment)
    return environment


async def get_user_membership(
    db: AsyncSession, user_id: str, org_id: str
) -> OrganizationMember | None:
    """Get user's membership in an organization."""
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def require_org_access(
    db: AsyncSession,
    user_id: str,
    org_id: str,
    min_role: OrganizationRole | None = None,
) -> OrganizationMember:
    """Require user has access to organization, optionally with minimum role."""
    membership = await get_user_membership(db, user_id, org_id)
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    if min_role:
        role_hierarchy = {
            OrganizationRole.MEMBER: 0,
            OrganizationRole.ADMIN: 1,
            OrganizationRole.OWNER: 2,
        }
        if role_hierarchy[membership.role] < role_hierarchy[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {min_role.value} role or higher",
            )

    return membership


# --- Organization endpoints ---


@router.post(
    "", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED
)
async def create_organization(
    data: OrganizationCreate,
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new organization. Creator becomes owner.

    This is a bootstrap endpoint that accepts OIDC tokens directly,
    since users need to create an org before they can do token exchange.
    """
    # Check org creation limit
    org_count_result = await db.execute(
        select(func.count(Organization.id)).where(
            Organization.created_by_id == current_user.id
        )
    )
    org_count = org_count_result.scalar() or 0
    if org_count >= Organization.MAX_ORGS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You can create at most {Organization.MAX_ORGS_PER_USER} organizations",
        )

    # Check if slug is taken
    existing = await db.execute(
        select(Organization).where(Organization.slug == data.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization slug already exists",
        )

    # Create organization
    org = Organization(
        name=data.name,
        slug=data.slug,
        description=data.description,
        created_by_id=current_user.id,
    )
    db.add(org)
    await db.flush()

    # Add creator as owner
    membership = OrganizationMember(
        organization_id=org.id,
        user_id=current_user.id,
        role=OrganizationRole.OWNER,
    )
    db.add(membership)

    # Create default environment
    environment = Environment(
        organization_id=org.id,
        name="Default",
        slug="default",
        description="Default environment",
    )
    db.add(environment)

    # Create personal environment for the creator
    await create_personal_environment(db, org.id, current_user)

    await db.commit()
    await db.refresh(org)

    return org


@router.get("/{org_id}", response_model=OrganizationDetailResponse)
async def get_organization(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get organization details."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(Organization)
        .options(
            selectinload(Organization.members), selectinload(Organization.environments)
        )
        .where(Organization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    return OrganizationDetailResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        member_count=len(org.members),
        environment_count=len(org.environments),
    )


@router.patch("/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    org_id: str,
    data: OrganizationUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update organization (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    if data.name is not None:
        org.name = data.name
    if data.description is not None:
        org.description = data.description

    await db.commit()
    await db.refresh(org)

    return org


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete organization (owner only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.OWNER
    )

    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    await db.delete(org)
    await db.commit()


# --- Member endpoints ---


@router.get("/{org_id}/members", response_model=list[MemberResponse])
async def list_members(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List organization members."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(OrganizationMember, User)
        .join(User, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
    )
    members = result.all()

    return [
        MemberResponse(
            id=membership.id,
            user_id=membership.user_id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role,
        )
        for membership, user in members
    ]


@router.patch("/{org_id}/members/{member_id}", response_model=MemberResponse)
async def update_member_role(
    org_id: str,
    member_id: str,
    data: MemberUpdateRole,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update member role (admin+ only, owner for owner changes)."""
    current_membership = await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Get target membership
    result = await db.execute(
        select(OrganizationMember, User)
        .join(User, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Member not found")

    membership, user = row

    # Only owners can promote to owner or demote owners
    if (
        data.role == OrganizationRole.OWNER or membership.role == OrganizationRole.OWNER
    ) and current_membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners can modify owner roles",
        )

    membership.role = data.role
    await db.commit()
    await db.refresh(membership)

    return MemberResponse(
        id=membership.id,
        user_id=membership.user_id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
    )


@router.delete("/{org_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    org_id: str,
    member_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove member from organization (admin+ only)."""
    current_membership = await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    membership = result.scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    # Can't remove owners unless you're owner
    if (
        membership.role == OrganizationRole.OWNER
        and current_membership.role != OrganizationRole.OWNER
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners can remove owners",
        )

    # Can't remove yourself if you're the last owner
    if (
        membership.user_id == current_user.id
        and membership.role == OrganizationRole.OWNER
    ):
        owner_count = await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == OrganizationRole.OWNER,
            )
        )
        if len(owner_count.scalars().all()) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove the last owner",
            )

    await db.delete(membership)
    await db.commit()


# --- Invite endpoints ---


@router.get("/{org_id}/invites", response_model=list[InviteResponse])
async def list_invites(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List pending invites (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(Invite, User)
        .outerjoin(User, Invite.invited_by_id == User.id)
        .where(Invite.organization_id == org_id, Invite.status == InviteStatus.PENDING)
    )
    invites = result.all()

    return [
        InviteResponse(
            id=invite.id,
            email=invite.email,
            role=invite.role,
            status=invite.status,
            invited_by_email=user.email if user else None,
        )
        for invite, user in invites
    ]


@router.post(
    "/{org_id}/invites",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    org_id: str,
    data: InviteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Invite a user to the organization (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Check if user is already a member
    existing_user = await db.execute(select(User).where(User.email == data.email))
    user = existing_user.scalar_one_or_none()
    if user:
        existing_membership = await get_user_membership(db, user.id, org_id)
        if existing_membership:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member",
            )

    # Check for existing pending invite
    existing_invite = await db.execute(
        select(Invite).where(
            Invite.organization_id == org_id,
            Invite.email == data.email,
            Invite.status == InviteStatus.PENDING,
        )
    )
    if existing_invite.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invite already pending for this email",
        )

    invite = Invite(
        organization_id=org_id,
        email=data.email,
        role=data.role,
        invited_by_id=current_user.id,
        status=InviteStatus.PENDING,
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)

    return InviteResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        invited_by_email=current_user.email,
    )


@router.delete("/{org_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invite(
    org_id: str,
    invite_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Cancel a pending invite (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(Invite).where(
            Invite.id == invite_id,
            Invite.organization_id == org_id,
            Invite.status == InviteStatus.PENDING,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    invite.status = InviteStatus.CANCELLED
    await db.commit()


@router.post("/invites/{invite_id}/accept", response_model=OrganizationResponse)
async def accept_invite(
    invite_id: str,
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Accept an invite (user accepting their own invite).

    This is a bootstrap endpoint that accepts OIDC tokens directly,
    since users might be accepting their first invite before having any orgs.
    """
    result = await db.execute(
        select(Invite)
        .options(selectinload(Invite.organization))
        .where(
            Invite.id == invite_id,
            Invite.email == current_user.email,
            Invite.status == InviteStatus.PENDING,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    # Create membership
    membership = OrganizationMember(
        organization_id=invite.organization_id,
        user_id=current_user.id,
        role=invite.role,
    )
    db.add(membership)

    # Create personal environment for the new member
    await create_personal_environment(db, invite.organization_id, current_user)

    invite.status = InviteStatus.ACCEPTED
    await db.commit()

    return invite.organization


@router.post("/invites/{invite_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invite(
    invite_id: str,
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Decline an invite.

    This is a bootstrap endpoint that accepts OIDC tokens directly.
    """
    result = await db.execute(
        select(Invite).where(
            Invite.id == invite_id,
            Invite.email == current_user.email,
            Invite.status == InviteStatus.PENDING,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    invite.status = InviteStatus.DECLINED
    await db.commit()


# --- Environment endpoints ---


@router.get("/{org_id}/environments", response_model=list[EnvironmentResponse])
async def list_environments(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List environments in organization."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(Environment).where(Environment.organization_id == org_id)
    )
    return result.scalars().all()


@router.post(
    "/{org_id}/environments",
    response_model=EnvironmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_environment(
    org_id: str,
    data: EnvironmentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create an environment (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Check environment limit
    environment_count_result = await db.execute(
        select(func.count(Environment.id)).where(Environment.organization_id == org_id)
    )
    environment_count = environment_count_result.scalar() or 0
    if environment_count >= Organization.MAX_ENVIRONMENTS_PER_ORG:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Organization can have at most {Organization.MAX_ENVIRONMENTS_PER_ORG} environments",
        )

    # Check if slug exists in this org
    existing = await db.execute(
        select(Environment).where(
            Environment.organization_id == org_id,
            Environment.slug == data.slug,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Environment slug already exists in this organization",
        )

    environment = Environment(
        organization_id=org_id,
        name=data.name,
        slug=data.slug,
        description=data.description,
    )
    db.add(environment)
    await db.commit()
    await db.refresh(environment)

    return environment


@router.get(
    "/{org_id}/environments/{environment_id}", response_model=EnvironmentResponse
)
async def get_environment(
    org_id: str,
    environment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get environment details."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    environment = result.scalar_one_or_none()
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    return environment


@router.patch(
    "/{org_id}/environments/{environment_id}", response_model=EnvironmentResponse
)
async def update_environment(
    org_id: str,
    environment_id: str,
    data: EnvironmentUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update environment (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    environment = result.scalar_one_or_none()
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    if data.name is not None:
        environment.name = data.name
    if data.description is not None:
        environment.description = data.description

    await db.commit()
    await db.refresh(environment)

    return environment


@router.delete(
    "/{org_id}/environments/{environment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_environment(
    org_id: str,
    environment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete environment (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    environment = result.scalar_one_or_none()
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check if it's the last environment
    environment_count = await db.execute(
        select(Environment).where(Environment.organization_id == org_id)
    )
    if len(environment_count.scalars().all()) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the last environment",
        )

    await db.delete(environment)
    await db.commit()


# --- API Key Schemas ---


class ApiKeyCreate(BaseModel):
    """Create an API key."""

    name: str


class ApiKeyResponse(BaseModel):
    """API key response (without the actual key)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    environment_id: str
    name: str
    key_prefix: str
    created_by_id: str | None
    created_at: str  # ISO format datetime
    last_used_at: str | None
    revoked_at: str | None

    @property
    def is_active(self) -> bool:
        """Check if the API key is active."""
        return self.revoked_at is None


class ApiKeyCreateResponse(ApiKeyResponse):
    """API key creation response (includes the full key once)."""

    key: str  # The full key, only returned on creation


# --- Target Root Schemas ---


class TargetRootCreate(BaseModel):
    """Create a target root."""

    name: str
    uri_prefix: str


class TargetRootUpdate(BaseModel):
    """Update a target root."""

    name: str | None = None
    uri_prefix: str | None = None


class TargetRootResponse(BaseModel):
    """Target root response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    environment_id: str
    name: str
    uri_prefix: str
    created_at: str  # ISO format datetime


# --- API Key endpoints ---


@router.get(
    "/{org_id}/environments/{environment_id}/api-keys",
    response_model=list[ApiKeyResponse],
)
async def list_api_keys(
    org_id: str,
    environment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_revoked: bool = False,
):
    """List API keys for an environment."""
    await require_org_access(db, current_user.id, org_id)

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    keys = await api_key_service.list_api_keys(
        db, environment_id, include_revoked=include_revoked
    )

    return [
        ApiKeyResponse(
            id=key.id,
            environment_id=key.environment_id,
            name=key.name,
            key_prefix=key.key_prefix,
            created_by_id=key.created_by_id,
            created_at=key.created_at.isoformat(),
            last_used_at=key.last_used_at.isoformat() if key.last_used_at else None,
            revoked_at=key.revoked_at.isoformat() if key.revoked_at else None,
        )
        for key in keys
    ]


@router.post(
    "/{org_id}/environments/{environment_id}/api-keys",
    response_model=ApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    org_id: str,
    environment_id: str,
    data: ApiKeyCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new API key for an environment (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    api_key, full_key = await api_key_service.create_api_key(
        db,
        environment_id=environment_id,
        name=data.name,
        created_by_id=current_user.id,
    )
    await db.commit()

    return ApiKeyCreateResponse(
        id=api_key.id,
        environment_id=api_key.environment_id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        created_by_id=api_key.created_by_id,
        created_at=api_key.created_at.isoformat(),
        last_used_at=None,
        revoked_at=None,
        key=full_key,
    )


@router.delete(
    "/{org_id}/environments/{environment_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_api_key(
    org_id: str,
    environment_id: str,
    key_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Revoke an API key (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Verify the key exists and belongs to this environment
    key = await api_key_service.get_api_key_by_id(db, key_id)
    if not key or key.environment_id != environment_id:
        raise HTTPException(status_code=404, detail="API key not found")

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    await api_key_service.revoke_api_key(db, key_id)
    await db.commit()


# --- Target Root endpoints ---


@router.get(
    "/{org_id}/environments/{environment_id}/target-roots",
    response_model=list[TargetRootResponse],
)
async def list_target_roots(
    org_id: str,
    environment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List target roots for an environment."""
    await require_org_access(db, current_user.id, org_id)

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    result = await db.execute(
        select(TargetRoot).where(TargetRoot.environment_id == environment_id)
    )
    roots = result.scalars().all()

    return [
        TargetRootResponse(
            id=root.id,
            environment_id=root.environment_id,
            name=root.name,
            uri_prefix=root.uri_prefix,
            created_at=root.created_at.isoformat(),
        )
        for root in roots
    ]


@router.post(
    "/{org_id}/environments/{environment_id}/target-roots",
    response_model=TargetRootResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_target_root(
    org_id: str,
    environment_id: str,
    data: TargetRootCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new target root for an environment (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check for duplicate name
    result = await db.execute(
        select(TargetRoot).where(
            TargetRoot.environment_id == environment_id,
            TargetRoot.name == data.name,
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target root with name '{data.name}' already exists",
        )

    target_root = TargetRoot(
        environment_id=environment_id,
        name=data.name,
        uri_prefix=data.uri_prefix,
    )
    db.add(target_root)
    await db.commit()
    await db.refresh(target_root)

    return TargetRootResponse(
        id=target_root.id,
        environment_id=target_root.environment_id,
        name=target_root.name,
        uri_prefix=target_root.uri_prefix,
        created_at=target_root.created_at.isoformat(),
    )


@router.get(
    "/{org_id}/environments/{environment_id}/target-roots/{root_id}",
    response_model=TargetRootResponse,
)
async def get_target_root(
    org_id: str,
    environment_id: str,
    root_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a specific target root."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(TargetRoot).where(
            TargetRoot.id == root_id,
            TargetRoot.environment_id == environment_id,
        )
    )
    target_root = result.scalar_one_or_none()
    if not target_root:
        raise HTTPException(status_code=404, detail="Target root not found")

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    return TargetRootResponse(
        id=target_root.id,
        environment_id=target_root.environment_id,
        name=target_root.name,
        uri_prefix=target_root.uri_prefix,
        created_at=target_root.created_at.isoformat(),
    )


@router.patch(
    "/{org_id}/environments/{environment_id}/target-roots/{root_id}",
    response_model=TargetRootResponse,
)
async def update_target_root(
    org_id: str,
    environment_id: str,
    root_id: str,
    data: TargetRootUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a target root (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(TargetRoot).where(
            TargetRoot.id == root_id,
            TargetRoot.environment_id == environment_id,
        )
    )
    target_root = result.scalar_one_or_none()
    if not target_root:
        raise HTTPException(status_code=404, detail="Target root not found")

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check for duplicate name if name is being changed
    if data.name is not None and data.name != target_root.name:
        result = await db.execute(
            select(TargetRoot).where(
                TargetRoot.environment_id == environment_id,
                TargetRoot.name == data.name,
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Target root with name '{data.name}' already exists",
            )
        target_root.name = data.name

    if data.uri_prefix is not None:
        target_root.uri_prefix = data.uri_prefix

    await db.commit()
    await db.refresh(target_root)

    return TargetRootResponse(
        id=target_root.id,
        environment_id=target_root.environment_id,
        name=target_root.name,
        uri_prefix=target_root.uri_prefix,
        created_at=target_root.created_at.isoformat(),
    )


@router.delete(
    "/{org_id}/environments/{environment_id}/target-roots/{root_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_target_root(
    org_id: str,
    environment_id: str,
    root_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a target root (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(TargetRoot).where(
            TargetRoot.id == root_id,
            TargetRoot.environment_id == environment_id,
        )
    )
    target_root = result.scalar_one_or_none()
    if not target_root:
        raise HTTPException(status_code=404, detail="Target root not found")

    # Verify environment belongs to org
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == org_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    await db.delete(target_root)
    await db.commit()
