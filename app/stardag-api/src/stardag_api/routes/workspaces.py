"""Workspace management routes (UI, requires auth).

The create workspace endpoint accepts OIDC tokens (bootstrap endpoint)
since users need to create a workspace before they can do token exchange.

Other endpoints require workspace-scoped internal tokens.
"""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from stardag_api.auth import get_current_user, get_current_user_flexible
from stardag_api.config import limits_settings
from stardag_api.db import get_db
from stardag_api.models import (
    Invite,
    InviteStatus,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
    TargetRoot,
    User,
    Environment,
)
from stardag_api.routes.workspace_access import (
    get_user_membership,
    require_workspace_access,
)
from stardag_api.routes.workspace_environments import (
    router as environments_router,
)
from stardag_api.services.email import get_email_service

router = APIRouter(prefix="/ui/workspaces", tags=["workspaces"])


# --- Schemas ---


class WorkspaceCreate(BaseModel):
    """Create a new workspace."""

    name: str
    slug: str
    description: str | None = None
    # Optional fields for shared workspace setup
    initial_environment_name: str | None = None  # Default: "Default"
    initial_environment_slug: str | None = None  # Default: "default"
    initial_target_root_name: str | None = None
    initial_target_root_uri: str | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9_-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError(
                "Slug must be lowercase alphanumeric with hyphens or underscores, "
                "cannot start or end with hyphen or underscore"
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

    id: UUID
    name: str
    slug: str
    description: str | None
    is_personal: bool = False


class WorkspaceDetailResponse(WorkspaceResponse):
    """Workspace with member count."""

    member_count: int
    environment_count: int


class MemberResponse(BaseModel):
    """Workspace member."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    email: str
    display_name: str | None
    role: WorkspaceRole


class MemberUpdateRole(BaseModel):
    """Update member role."""

    role: WorkspaceRole


class InviteCreate(BaseModel):
    """Create an invite."""

    email: str
    role: WorkspaceRole = WorkspaceRole.MEMBER


class InviteResponse(BaseModel):
    """Invite response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: WorkspaceRole
    status: InviteStatus
    invited_by_email: str | None


# --- Workspace endpoints ---


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    data: WorkspaceCreate,
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new workspace. Creator becomes owner.

    This is a bootstrap endpoint that accepts OIDC tokens directly,
    since users need to create a workspace before they can do token exchange.
    """
    # Check workspace creation limit
    workspace_count_result = await db.execute(
        select(func.count(Workspace.id)).where(
            Workspace.created_by_id == current_user.id
        )
    )
    workspace_count = workspace_count_result.scalar() or 0
    max_workspaces = limits_settings.max_workspaces_per_user
    if max_workspaces is not None and workspace_count >= max_workspaces:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You can create at most {max_workspaces} workspaces",
        )

    # Check if slug is taken
    existing = await db.execute(select(Workspace).where(Workspace.slug == data.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace slug already exists",
        )

    # Create workspace
    workspace = Workspace(
        name=data.name,
        slug=data.slug,
        description=data.description,
        created_by_id=current_user.id,
        is_personal=False,  # Explicitly not personal (user-created shared workspace)
    )
    db.add(workspace)
    await db.flush()

    # Add creator as owner
    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=current_user.id,
        role=WorkspaceRole.OWNER,
    )
    db.add(membership)

    # Create initial environment (using provided names or defaults)
    env_name = data.initial_environment_name or "Default"
    env_slug = data.initial_environment_slug or "default"
    environment = Environment(
        workspace_id=workspace.id,
        name=env_name,
        slug=env_slug,
        description=f"{env_name} environment",
    )
    db.add(environment)
    await db.flush()

    # Create initial target root if provided
    if data.initial_target_root_name and data.initial_target_root_uri:
        target_root = TargetRoot(
            environment_id=environment.id,
            name=data.initial_target_root_name,
            uri_prefix=data.initial_target_root_uri,
        )
        db.add(target_root)

    await db.commit()
    await db.refresh(workspace)

    return workspace


@router.get("/{workspace_id}", response_model=WorkspaceDetailResponse)
async def get_workspace(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get workspace details."""
    await require_workspace_access(db, current_user.id, workspace_id)

    result = await db.execute(
        select(Workspace)
        .options(selectinload(Workspace.members), selectinload(Workspace.environments))
        .where(Workspace.id == workspace_id)
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return WorkspaceDetailResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        member_count=len(workspace.members),
        environment_count=len(workspace.environments),
    )


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    data: WorkspaceUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update workspace (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
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


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete workspace (owner only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.OWNER
    )

    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.delete(workspace)
    await db.commit()


# --- Member endpoints ---


@router.get("/{workspace_id}/members", response_model=list[MemberResponse])
async def list_members(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List workspace members."""
    await require_workspace_access(db, current_user.id, workspace_id)

    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
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


@router.patch("/{workspace_id}/members/{member_id}", response_model=MemberResponse)
async def update_member_role(
    workspace_id: UUID,
    member_id: UUID,
    data: MemberUpdateRole,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update member role (admin+ only, owner for owner changes)."""
    current_membership = await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    # Get target membership
    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .where(
            WorkspaceMember.id == member_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Member not found")

    membership, user = row

    # Only owners can promote to owner or demote owners
    if (
        data.role == WorkspaceRole.OWNER or membership.role == WorkspaceRole.OWNER
    ) and current_membership.role != WorkspaceRole.OWNER:
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


@router.delete(
    "/{workspace_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    workspace_id: UUID,
    member_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove member from workspace (admin+ only)."""
    current_membership = await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.id == member_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    membership = result.scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    # Can't remove owners unless you're owner
    if (
        membership.role == WorkspaceRole.OWNER
        and current_membership.role != WorkspaceRole.OWNER
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners can remove owners",
        )

    # Can't remove yourself if you're the last owner
    if membership.user_id == current_user.id and membership.role == WorkspaceRole.OWNER:
        owner_count = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == WorkspaceRole.OWNER,
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


@router.get("/{workspace_id}/invites", response_model=list[InviteResponse])
async def list_invites(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List pending invites (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    result = await db.execute(
        select(Invite, User)
        .outerjoin(User, Invite.invited_by_id == User.id)
        .where(
            Invite.workspace_id == workspace_id, Invite.status == InviteStatus.PENDING
        )
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
    "/{workspace_id}/invites",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    workspace_id: UUID,
    data: InviteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Invite a user to the workspace (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    # Get workspace and check if personal (no invites allowed)
    workspace_result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )
    workspace = workspace_result.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    if workspace.is_personal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot invite members to a personal workspace",
        )

    # Check if user is already a member
    existing_user = await db.execute(select(User).where(User.email == data.email))
    user = existing_user.scalar_one_or_none()
    if user:
        existing_membership = await get_user_membership(db, user.id, workspace_id)
        if existing_membership:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member",
            )

    # Check for existing pending invite
    existing_invite = await db.execute(
        select(Invite).where(
            Invite.workspace_id == workspace_id,
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
        workspace_id=workspace_id,
        email=data.email,
        role=data.role,
        invited_by_id=current_user.id,
        status=InviteStatus.PENDING,
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)

    # Send invitation email (fire-and-forget, don't block on failure)
    email_service = get_email_service()
    is_new_user = user is None  # user lookup happened earlier in the function
    inviter_name = current_user.display_name or current_user.email.split("@")[0]

    await email_service.send_invite_email(
        to_email=data.email,
        workspace_name=workspace.name,
        inviter_name=inviter_name,
        inviter_email=current_user.email,
        role=data.role.value.title(),  # "member" -> "Member"
        invite_link=f"{email_service.app_url}/invites",
        is_new_user=is_new_user,
    )

    return InviteResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        invited_by_email=current_user.email,
    )


@router.delete(
    "/{workspace_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def cancel_invite(
    workspace_id: UUID,
    invite_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Cancel a pending invite (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    result = await db.execute(
        select(Invite).where(
            Invite.id == invite_id,
            Invite.workspace_id == workspace_id,
            Invite.status == InviteStatus.PENDING,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    invite.status = InviteStatus.CANCELLED
    await db.commit()


@router.post("/invites/{invite_id}/accept", response_model=WorkspaceResponse)
async def accept_invite(
    invite_id: UUID,
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Accept an invite (user accepting their own invite).

    This is a bootstrap endpoint that accepts OIDC tokens directly,
    since users might be accepting their first invite before having any workspaces.
    """
    result = await db.execute(
        select(Invite)
        .options(selectinload(Invite.workspace))
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
    membership = WorkspaceMember(
        workspace_id=invite.workspace_id,
        user_id=current_user.id,
        role=invite.role,
    )
    db.add(membership)

    invite.status = InviteStatus.ACCEPTED
    await db.commit()

    return invite.workspace


@router.post("/invites/{invite_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invite(
    invite_id: UUID,
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


# Environments, their API keys and target roots: the same prefix, in
# routes/workspace_environments.py (module-size rule).
router.include_router(environments_router)
