"""Environment management routes of a workspace (UI, requires auth):
environments, their API keys and their target roots.

Split from ``routes/workspaces.py`` (module-size rule); included by its
router, so the paths are unchanged (``/ui/workspaces/...``).
"""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import get_current_user
from stardag_api.config import limits_settings
from stardag_api.db import get_db
from stardag_api.models import Environment, TargetRoot, User, WorkspaceRole
from stardag_api.routes.workspace_access import require_workspace_access
from stardag_api.services import api_keys as api_key_service

router = APIRouter()


# --- Environment schemas ---


class EnvironmentCreate(BaseModel):
    """Create an environment."""

    name: str
    slug: str
    description: str | None = None

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


class EnvironmentUpdate(BaseModel):
    """Update an environment."""

    name: str | None = None
    description: str | None = None


class EnvironmentResponse(BaseModel):
    """Environment response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    description: str | None
    owner_id: UUID | None = None  # Deprecated: was used for personal environments


# --- Environment endpoints ---


@router.get("/{workspace_id}/environments", response_model=list[EnvironmentResponse])
async def list_environments(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List environments in workspace."""
    await require_workspace_access(db, current_user.id, workspace_id)

    result = await db.execute(
        select(Environment).where(Environment.workspace_id == workspace_id)
    )
    return result.scalars().all()


@router.post(
    "/{workspace_id}/environments",
    response_model=EnvironmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_environment(
    workspace_id: UUID,
    data: EnvironmentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create an environment (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    # Check environment limit
    environment_count_result = await db.execute(
        select(func.count(Environment.id)).where(
            Environment.workspace_id == workspace_id
        )
    )
    environment_count = environment_count_result.scalar() or 0
    max_environments = limits_settings.max_environments_per_workspace
    if max_environments is not None and environment_count >= max_environments:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Workspace can have at most {max_environments} environments",
        )

    # Check if slug exists in this workspace
    existing = await db.execute(
        select(Environment).where(
            Environment.workspace_id == workspace_id,
            Environment.slug == data.slug,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Environment slug already exists in this workspace",
        )

    environment = Environment(
        workspace_id=workspace_id,
        name=data.name,
        slug=data.slug,
        description=data.description,
    )
    db.add(environment)
    await db.commit()
    await db.refresh(environment)

    return environment


@router.get(
    "/{workspace_id}/environments/{environment_id}", response_model=EnvironmentResponse
)
async def get_environment(
    workspace_id: UUID,
    environment_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get environment details."""
    await require_workspace_access(db, current_user.id, workspace_id)

    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
        )
    )
    environment = result.scalar_one_or_none()
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    return environment


@router.patch(
    "/{workspace_id}/environments/{environment_id}", response_model=EnvironmentResponse
)
async def update_environment(
    workspace_id: UUID,
    environment_id: UUID,
    data: EnvironmentUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update environment (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_environment(
    workspace_id: UUID,
    environment_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete environment (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
        )
    )
    environment = result.scalar_one_or_none()
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check if it's the last environment
    environment_count = await db.execute(
        select(Environment).where(Environment.workspace_id == workspace_id)
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

    id: UUID
    environment_id: UUID
    name: str
    key_prefix: str
    created_by_id: UUID | None
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

    id: UUID
    environment_id: UUID
    name: str
    uri_prefix: str
    created_at: str  # ISO format datetime


# --- API Key endpoints ---


@router.get(
    "/{workspace_id}/environments/{environment_id}/api-keys",
    response_model=list[ApiKeyResponse],
)
async def list_api_keys(
    workspace_id: UUID,
    environment_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_revoked: bool = False,
):
    """List API keys for an environment."""
    await require_workspace_access(db, current_user.id, workspace_id)

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}/api-keys",
    response_model=ApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    workspace_id: UUID,
    environment_id: UUID,
    data: ApiKeyCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new API key for an environment (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_api_key(
    workspace_id: UUID,
    environment_id: UUID,
    key_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Revoke an API key (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    # Verify the key exists and belongs to this environment
    key = await api_key_service.get_api_key_by_id(db, key_id)
    if not key or key.environment_id != environment_id:
        raise HTTPException(status_code=404, detail="API key not found")

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    await api_key_service.revoke_api_key(db, key_id)
    await db.commit()


# --- Target Root endpoints ---


@router.get(
    "/{workspace_id}/environments/{environment_id}/target-roots",
    response_model=list[TargetRootResponse],
)
async def list_target_roots(
    workspace_id: UUID,
    environment_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List target roots for an environment."""
    await require_workspace_access(db, current_user.id, workspace_id)

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}/target-roots",
    response_model=TargetRootResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_target_root(
    workspace_id: UUID,
    environment_id: UUID,
    data: TargetRootCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new target root for an environment (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
    )

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}/target-roots/{root_id}",
    response_model=TargetRootResponse,
)
async def get_target_root(
    workspace_id: UUID,
    environment_id: UUID,
    root_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a specific target root."""
    await require_workspace_access(db, current_user.id, workspace_id)

    result = await db.execute(
        select(TargetRoot).where(
            TargetRoot.id == root_id,
            TargetRoot.environment_id == environment_id,
        )
    )
    target_root = result.scalar_one_or_none()
    if not target_root:
        raise HTTPException(status_code=404, detail="Target root not found")

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}/target-roots/{root_id}",
    response_model=TargetRootResponse,
)
async def update_target_root(
    workspace_id: UUID,
    environment_id: UUID,
    root_id: UUID,
    data: TargetRootUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a target root (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
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

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
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
    "/{workspace_id}/environments/{environment_id}/target-roots/{root_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_target_root(
    workspace_id: UUID,
    environment_id: UUID,
    root_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a target root (admin+ only)."""
    await require_workspace_access(
        db, current_user.id, workspace_id, min_role=WorkspaceRole.ADMIN
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

    # Verify environment belongs to workspace
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Environment not found")

    await db.delete(target_root)
    await db.commit()
