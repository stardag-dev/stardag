"""Organization management routes (UI, requires auth)."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from stardag_api.auth import get_current_user
from stardag_api.db import get_db
from stardag_api.models import (
    Invite,
    InviteStatus,
    Organization,
    OrganizationMember,
    OrganizationRole,
    User,
    Workspace,
)

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
    workspace_count: int


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


class WorkspaceCreate(BaseModel):
    """Create a workspace."""

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


class WorkspaceUpdate(BaseModel):
    """Update a workspace."""

    name: str | None = None
    description: str | None = None


class WorkspaceResponse(BaseModel):
    """Workspace response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    slug: str
    description: str | None


# --- Helper functions ---


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
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new organization. Creator becomes owner."""
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

    # Create default workspace
    workspace = Workspace(
        organization_id=org.id,
        name="Default",
        slug="default",
        description="Default workspace",
    )
    db.add(workspace)

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
            selectinload(Organization.members), selectinload(Organization.workspaces)
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
        workspace_count=len(org.workspaces),
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
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Accept an invite (user accepting their own invite)."""
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

    invite.status = InviteStatus.ACCEPTED
    await db.commit()

    return invite.organization


@router.post("/invites/{invite_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invite(
    invite_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Decline an invite."""
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


# --- Workspace endpoints ---


@router.get("/{org_id}/workspaces", response_model=list[WorkspaceResponse])
async def list_workspaces(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List workspaces in organization."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(Workspace).where(Workspace.organization_id == org_id)
    )
    return result.scalars().all()


@router.post(
    "/{org_id}/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    org_id: str,
    data: WorkspaceCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a workspace (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    # Check workspace limit
    workspace_count_result = await db.execute(
        select(func.count(Workspace.id)).where(Workspace.organization_id == org_id)
    )
    workspace_count = workspace_count_result.scalar() or 0
    if workspace_count >= Organization.MAX_WORKSPACES_PER_ORG:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Organization can have at most {Organization.MAX_WORKSPACES_PER_ORG} workspaces",
        )

    # Check if slug exists in this org
    existing = await db.execute(
        select(Workspace).where(
            Workspace.organization_id == org_id,
            Workspace.slug == data.slug,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace slug already exists in this organization",
        )

    workspace = Workspace(
        organization_id=org_id,
        name=data.name,
        slug=data.slug,
        description=data.description,
    )
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)

    return workspace


@router.get("/{org_id}/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    org_id: str,
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get workspace details."""
    await require_org_access(db, current_user.id, org_id)

    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.organization_id == org_id,
        )
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return workspace


@router.patch("/{org_id}/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    org_id: str,
    workspace_id: str,
    data: WorkspaceUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update workspace (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.organization_id == org_id,
        )
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if data.name is not None:
        workspace.name = data.name
    if data.description is not None:
        workspace.description = data.description

    await db.commit()
    await db.refresh(workspace)

    return workspace


@router.delete(
    "/{org_id}/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_workspace(
    org_id: str,
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete workspace (admin+ only)."""
    await require_org_access(
        db, current_user.id, org_id, min_role=OrganizationRole.ADMIN
    )

    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.organization_id == org_id,
        )
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Check if it's the last workspace
    workspace_count = await db.execute(
        select(Workspace).where(Workspace.organization_id == org_id)
    )
    if len(workspace_count.scalars().all()) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the last workspace",
        )

    await db.delete(workspace)
    await db.commit()
