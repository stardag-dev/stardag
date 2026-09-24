"""Workspace membership checks shared by the workspace UI routes."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import WorkspaceMember, WorkspaceRole


async def get_user_membership(
    db: AsyncSession, user_id: UUID, workspace_id: UUID
) -> WorkspaceMember | None:
    """Get user's membership in a workspace."""
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    return result.scalar_one_or_none()


async def require_workspace_access(
    db: AsyncSession,
    user_id: UUID,
    workspace_id: UUID,
    min_role: WorkspaceRole | None = None,
) -> WorkspaceMember:
    """Require user has access to workspace, optionally with minimum role."""
    membership = await get_user_membership(db, user_id, workspace_id)
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )

    if min_role:
        role_hierarchy = {
            WorkspaceRole.MEMBER: 0,
            WorkspaceRole.ADMIN: 1,
            WorkspaceRole.OWNER: 2,
        }
        if role_hierarchy[membership.role] < role_hierarchy[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {min_role.value} role or higher",
            )

    return membership
