"""Tests for primary workspace/environment bootstrap (local auth mode).

Covers the startup provisioning driven by AUTH_PRIMARY_WORKSPACE_NAME /
AUTH_PRIMARY_WORKSPACE_ENVIRONMENT (see
stardag_api.services.local_auth.ensure_primary_workspace).
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.config import auth_settings
from stardag_api.models import Environment, User, Workspace, WorkspaceMember
from stardag_api.models.enums import WorkspaceRole
from stardag_api.services.local_auth import (
    ensure_bootstrap_admin,
    ensure_primary_workspace,
)

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "adm1n-passw0rd"


@pytest.fixture
def local_mode_with_admin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth_settings, "mode", "local")
    monkeypatch.setattr(auth_settings, "bootstrap_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(auth_settings, "bootstrap_admin_password", ADMIN_PASSWORD)
    monkeypatch.setattr(auth_settings, "primary_workspace_name", None)
    monkeypatch.setattr(auth_settings, "primary_workspace_environment", "main")


async def _get_admin(session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.email == ADMIN_EMAIL))
    return result.scalar_one()


async def _workspace_envs(session: AsyncSession, workspace: Workspace) -> list[str]:
    result = await session.execute(
        select(Environment.slug).where(Environment.workspace_id == workspace.id)
    )
    return sorted(result.scalars().all())


@pytest.mark.asyncio
async def test_primary_workspace_shared_name(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    local_mode_with_admin,
):
    """A named primary workspace is created as non-personal, with the
    bootstrap admin as owner and the primary environment inside it."""
    monkeypatch.setattr(auth_settings, "primary_workspace_name", "Acme Corp")

    await ensure_bootstrap_admin(async_session)
    await ensure_primary_workspace(async_session)

    admin = await _get_admin(async_session)
    result = await async_session.execute(
        select(Workspace).where(Workspace.name == "Acme Corp")
    )
    workspace = result.scalar_one()
    assert workspace.slug == "acme-corp"
    assert workspace.is_personal is False
    assert workspace.created_by_id == admin.id

    membership_result = await async_session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == admin.id,
        )
    )
    membership = membership_result.scalar_one()
    assert membership.role == WorkspaceRole.OWNER

    assert await _workspace_envs(async_session, workspace) == ["main"]


@pytest.mark.asyncio
async def test_primary_workspace_shared_name_idempotent(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    local_mode_with_admin,
):
    """Re-running on restart creates nothing new."""
    monkeypatch.setattr(auth_settings, "primary_workspace_name", "Acme Corp")

    await ensure_bootstrap_admin(async_session)
    await ensure_primary_workspace(async_session)
    await ensure_primary_workspace(async_session)

    admin = await _get_admin(async_session)
    workspaces = (
        (
            await async_session.execute(
                select(Workspace).where(Workspace.name == "Acme Corp")
            )
        )
        .scalars()
        .all()
    )
    assert len(workspaces) == 1

    memberships = (
        (
            await async_session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspaces[0].id,
                    WorkspaceMember.user_id == admin.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(memberships) == 1
    assert await _workspace_envs(async_session, workspaces[0]) == ["main"]


@pytest.mark.asyncio
async def test_primary_environment_in_personal_workspace(
    async_session: AsyncSession,
    local_mode_with_admin,
):
    """Without a primary workspace name, the primary environment lands in
    the bootstrap admin's personal workspace (next to the default 'local')."""
    await ensure_bootstrap_admin(async_session)
    await ensure_primary_workspace(async_session)

    admin = await _get_admin(async_session)
    result = await async_session.execute(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            WorkspaceMember.user_id == admin.id,
            Workspace.is_personal.is_(True),
        )
    )
    personal = result.scalars().one()
    assert await _workspace_envs(async_session, personal) == ["local", "main"]

    # Idempotent on restart
    await ensure_primary_workspace(async_session)
    assert await _workspace_envs(async_session, personal) == ["local", "main"]


@pytest.mark.asyncio
async def test_primary_workspace_existing_membership_not_demoted(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    local_mode_with_admin,
):
    """An existing (manually adjusted) membership is left untouched."""
    monkeypatch.setattr(auth_settings, "primary_workspace_name", "Acme Corp")
    await ensure_bootstrap_admin(async_session)
    await ensure_primary_workspace(async_session)

    admin = await _get_admin(async_session)
    workspace = (
        await async_session.execute(
            select(Workspace).where(Workspace.name == "Acme Corp")
        )
    ).scalar_one()
    membership = (
        await async_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == admin.id,
            )
        )
    ).scalar_one()
    membership.role = WorkspaceRole.MEMBER
    await async_session.commit()

    await ensure_primary_workspace(async_session)
    await async_session.refresh(membership)
    assert membership.role == WorkspaceRole.MEMBER


@pytest.mark.asyncio
async def test_primary_workspace_noop_in_oidc_mode(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(auth_settings, "mode", "oidc")
    monkeypatch.setattr(auth_settings, "primary_workspace_name", "Acme Corp")
    monkeypatch.setattr(auth_settings, "primary_workspace_environment", "main")

    await ensure_primary_workspace(async_session)

    result = await async_session.execute(
        select(Workspace).where(Workspace.name == "Acme Corp")
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_primary_workspace_noop_without_bootstrap_admin(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(auth_settings, "mode", "local")
    monkeypatch.setattr(auth_settings, "bootstrap_admin_email", None)
    monkeypatch.setattr(auth_settings, "bootstrap_admin_password", None)
    monkeypatch.setattr(auth_settings, "primary_workspace_name", "Acme Corp")
    monkeypatch.setattr(auth_settings, "primary_workspace_environment", "main")

    await ensure_primary_workspace(async_session)

    result = await async_session.execute(
        select(Workspace).where(Workspace.name == "Acme Corp")
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_primary_environment_disabled_with_empty_string(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    local_mode_with_admin,
):
    """AUTH_PRIMARY_WORKSPACE_ENVIRONMENT="" disables environment creation
    (workspace bootstrap still applies when a name is configured)."""
    monkeypatch.setattr(auth_settings, "primary_workspace_name", "Acme Corp")
    monkeypatch.setattr(auth_settings, "primary_workspace_environment", "")

    await ensure_bootstrap_admin(async_session)
    await ensure_primary_workspace(async_session)

    workspace = (
        await async_session.execute(
            select(Workspace).where(Workspace.name == "Acme Corp")
        )
    ).scalar_one()
    assert await _workspace_envs(async_session, workspace) == []
