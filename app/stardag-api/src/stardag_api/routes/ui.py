"""UI-specific routes for bootstrap (OIDC tokens) and authenticated operations.

Bootstrap endpoints (/me, /me/invites) accept OIDC tokens directly because
they are needed before the user can select a workspace for token exchange.

Other UI endpoints require workspace-scoped internal tokens.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import get_current_user_flexible
from stardag_api.db import get_db
from stardag_api.models import (
    Invite,
    InviteStatus,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
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


class WorkspaceSummary(BaseModel):
    """Summary of a workspace for listing."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    role: WorkspaceRole


class UserProfileWithWorkspacesResponse(BaseModel):
    """User profile with their workspaces."""

    user: UserProfileResponse
    workspaces: list[WorkspaceSummary]


class PendingInviteResponse(BaseModel):
    """Pending invite for current user."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    workspace_name: str
    role: WorkspaceRole
    invited_by_email: str | None


# --- Bootstrap Endpoints (accept OIDC tokens) ---


@router.get("/me", response_model=UserProfileWithWorkspacesResponse)
async def get_current_user_profile(
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the current user's profile and workspaces.

    This is a bootstrap endpoint that accepts OIDC tokens directly.
    It returns the user's workspaces so they can select one for token exchange.
    """
    # Get user's workspace memberships
    result = await db.execute(
        select(WorkspaceMember, Workspace)
        .join(Workspace, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == current_user.id)
    )
    memberships = result.all()

    workspaces = [
        WorkspaceSummary(
            id=ws.id,
            name=ws.name,
            slug=ws.slug,
            role=membership.role,
        )
        for membership, ws in memberships
    ]

    return UserProfileWithWorkspacesResponse(
        user=UserProfileResponse(
            id=current_user.id,
            external_id=current_user.external_id,
            email=current_user.email,
            display_name=current_user.display_name,
        ),
        workspaces=workspaces,
    )


@router.get("/me/invites", response_model=list[PendingInviteResponse])
async def get_pending_invites(
    current_user: Annotated[User, Depends(get_current_user_flexible)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get pending invites for the current user.

    This is a bootstrap endpoint that accepts OIDC tokens directly.
    """
    result = await db.execute(
        select(Invite, Workspace, User)
        .join(Workspace, Invite.workspace_id == Workspace.id)
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
            workspace_id=invite.workspace_id,
            workspace_name=ws.name,
            role=invite.role,
            invited_by_email=invited_by.email if invited_by else None,
        )
        for invite, ws, invited_by in invites
    ]
