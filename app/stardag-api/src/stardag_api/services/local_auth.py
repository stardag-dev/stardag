"""Local (email/password) authentication service.

Only active when AUTH_MODE=local. Users authenticate directly against the
API, which mints session tokens (see stardag_api.auth.tokens); no external
identity provider is involved.
"""

import logging
import re
import secrets as _secrets
import time
from collections import deque
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.passwords import (
    hash_password_async,
    validate_password_policy,
    verify_password_async,
)
from stardag_api.config import auth_settings
from stardag_api.models import (
    Environment,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)

logger = logging.getLogger(__name__)


class LoginRateLimiter:
    """In-process sliding-window rate limiter for login attempts.

    Sufficient for single-container deployments (the self-hosted target for
    local auth mode); multi-container deployments rate-limit per container,
    which still bounds attack throughput per replica.

    Memory is bounded even though keys are attacker-chosen (unauthenticated
    email/IP): a key is dropped as soon as its window empties, and once the
    number of tracked keys exceeds ``sweep_threshold`` every check first
    sweeps out fully-expired entries.
    """

    def __init__(
        self,
        max_attempts: int = 10,
        window_seconds: float = 300.0,
        sweep_threshold: int = 10_000,
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.sweep_threshold = sweep_threshold
        self._attempts: dict[str, deque[float]] = {}

    def check(self, key: str) -> bool:
        """Record an attempt for key; returns False when rate-limited."""
        now = time.monotonic()
        if len(self._attempts) > self.sweep_threshold:
            self._sweep(now)
        attempts = self._attempts.get(key)
        if attempts is not None:
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            if not attempts:
                # Window fully expired: drop the key (recreated below).
                del self._attempts[key]
                attempts = None
        if attempts is not None and len(attempts) >= self.max_attempts:
            return False
        if attempts is None:
            attempts = deque()
            self._attempts[key] = attempts
        attempts.append(now)
        return True

    def reset(self, key: str) -> None:
        """Clear attempts for key (e.g. after successful login)."""
        self._attempts.pop(key, None)

    def _sweep(self, now: float) -> None:
        """Drop all keys whose windows have fully expired (O(keys))."""
        cutoff = now - self.window_seconds
        expired = [
            key for key, dq in self._attempts.items() if not dq or dq[-1] <= cutoff
        ]
        for key in expired:
            del self._attempts[key]


# Two layers: per email+IP (tight, protects against a single source) and per
# email only (looser, so rotating spoofable client IPs can't brute-force one
# account while staying loose enough that a shared/NATed office doesn't lock
# a user out).
login_rate_limiter = LoginRateLimiter()
login_email_rate_limiter = LoginRateLimiter(max_attempts=30, window_seconds=900.0)


async def authenticate_local_user(
    db: AsyncSession, email: str, password: str
) -> User | None:
    """Verify email/password; returns the user on success, else None.

    Constant-cost on unknown email / OIDC-only user (dummy bcrypt verify)
    so response timing doesn't reveal account existence.
    """
    result = await db.execute(
        select(User).where(func.lower(User.email) == email.lower())
    )
    user = result.scalar_one_or_none()
    password_hash = user.password_hash if user is not None else None
    if not await verify_password_async(password, password_hash):
        return None
    return user


async def create_local_user(
    db: AsyncSession,
    email: str,
    password: str,
    display_name: str | None = None,
) -> User:
    """Create a local-auth user with a personal workspace.

    Raises:
        PasswordPolicyError: password fails policy
        ValueError: email already registered
    """
    from stardag_api.auth.dependencies import create_personal_workspace_for_user

    validate_password_policy(password)

    result = await db.execute(
        select(User).where(func.lower(User.email) == email.lower())
    )
    if result.scalar_one_or_none() is not None:
        raise ValueError("Email already registered")

    password_hash = await hash_password_async(password)
    try:
        user = User(
            # Local users have no IdP subject; external_id is NOT NULL +
            # unique, so use a stable synthetic value.
            external_id=f"local:{uuid4()}",
            email=email,
            display_name=display_name,
            password_hash=password_hash,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
        await create_personal_workspace_for_user(db, user)
        await db.commit()
        logger.info("Created local user %s", user.id)
        return user
    except IntegrityError as e:
        await db.rollback()
        raise ValueError("Email already registered") from e


async def ensure_bootstrap_admin(db: AsyncSession) -> None:
    """Idempotently create the bootstrap admin user (local mode startup).

    - No user with the configured email: create it (with personal workspace).
    - User exists without a password (e.g. pre-created via invite): set the
      bootstrap password.
    - User exists with a password: leave untouched — never overwrite a
      password that may have been changed since bootstrap.

    Raises:
        PasswordPolicyError: the configured bootstrap password fails the
            password policy (only when a password would actually be set).
    """
    email = auth_settings.bootstrap_admin_email
    password = auth_settings.bootstrap_admin_password
    if not email or not password:
        return

    result = await db.execute(
        select(User).where(func.lower(User.email) == email.lower())
    )
    user = result.scalar_one_or_none()
    if user is None:
        await create_local_user(db, email=email, password=password)
        logger.info("Bootstrap admin created: %s", email)
    elif user.password_hash is None:
        # Same policy as /auth/register (create_local_user validates in the
        # branch above): reject weak passwords and >72 bytes, which bcrypt
        # would otherwise silently truncate.
        validate_password_policy(password)
        user.password_hash = await hash_password_async(password)
        await db.commit()
        logger.info("Bootstrap admin password set for existing user: %s", email)
    else:
        logger.debug("Bootstrap admin already provisioned: %s", email)


def _slugify(name: str) -> str:
    """Derive a workspace/environment slug from a display name.

    Same derivation as personal-workspace creation: lowercase, non-alphanumeric
    runs collapsed to hyphens, trimmed to 50 chars.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]


async def _ensure_environment(
    db: AsyncSession, workspace: Workspace, env_name: str
) -> None:
    """Idempotently ensure an environment named ``env_name`` in ``workspace``."""
    env_slug = _slugify(env_name)
    if not env_slug:
        return
    result = await db.execute(
        select(Environment).where(
            Environment.workspace_id == workspace.id,
            Environment.slug == env_slug,
        )
    )
    if result.scalar_one_or_none() is not None:
        return
    db.add(
        Environment(
            workspace_id=workspace.id,
            name=env_name,
            slug=env_slug,
        )
    )
    logger.info("Created environment %r in workspace %s", env_slug, workspace.slug)


async def ensure_primary_workspace(db: AsyncSession) -> None:
    """Idempotently provision the primary workspace/environment (local mode).

    Driven by ``AUTH_PRIMARY_WORKSPACE_NAME`` / ``AUTH_PRIMARY_WORKSPACE_ENVIRONMENT``
    and anchored on the bootstrap admin (``ensure_bootstrap_admin`` must have
    run first):

    - ``primary_workspace_name`` set: ensure a shared (non-personal)
      workspace with that name exists, with the bootstrap admin as owner,
      and the primary environment in it.
    - ``primary_workspace_name`` unset: ensure the primary environment in
      the bootstrap admin's *personal* workspace instead (the typical
      single-user deployment).

    Safe to run on every startup: existing workspaces, memberships, and
    environments are matched (by name/slug) before anything is created, and
    nothing is ever renamed, demoted, or deleted. No-op outside local auth
    mode (in OIDC mode there is no known admin at startup; the CLI's
    ``self-host connect`` flow provisions the workspace via the API instead).
    """
    if auth_settings.mode != "local":
        return
    workspace_name = auth_settings.primary_workspace_name
    environment_name = auth_settings.primary_workspace_environment
    if not workspace_name and not environment_name:
        return
    admin_email = auth_settings.bootstrap_admin_email
    if not admin_email:
        return
    result = await db.execute(
        select(User).where(func.lower(User.email) == admin_email.lower())
    )
    admin = result.scalar_one_or_none()
    if admin is None:
        logger.warning(
            "Primary workspace bootstrap skipped: bootstrap admin %s not found",
            admin_email,
        )
        return

    if workspace_name:
        workspace = await _ensure_shared_workspace(db, admin, workspace_name)
    else:
        result = await db.execute(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                WorkspaceMember.user_id == admin.id,
                Workspace.is_personal.is_(True),
            )
        )
        workspace = result.scalars().first()
        if workspace is None:
            logger.warning(
                "Primary environment bootstrap skipped: no personal workspace "
                "for bootstrap admin"
            )
            return

    if environment_name:
        await _ensure_environment(db, workspace, environment_name)
    await db.commit()


async def _ensure_shared_workspace(
    db: AsyncSession, admin: User, name: str
) -> Workspace:
    """Find or create the shared primary workspace; ensure admin membership.

    Matched by exact name (among non-personal workspaces) first — the
    stable idempotency key across restarts — then by derived slug.
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.name == name,
            Workspace.is_personal.is_(False),
        )
    )
    workspace = result.scalars().first()

    if workspace is None:
        base_slug = _slugify(name) or "primary"
        slug = base_slug
        for hex_nbytes in [2, 2, 3, 3, 4]:
            slug_exists = await db.execute(
                select(Workspace).where(Workspace.slug == slug)
            )
            if not slug_exists.scalar_one_or_none():
                break
            slug = f"{base_slug}-{_secrets.token_hex(hex_nbytes)}"
        else:
            raise RuntimeError("Failed to generate unique workspace slug")

        workspace = Workspace(
            name=name,
            slug=slug,
            is_personal=False,
            created_by_id=admin.id,
        )
        db.add(workspace)
        await db.flush()
        logger.info("Created primary workspace %r (%s)", name, slug)

    membership_result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == admin.id,
        )
    )
    if membership_result.scalar_one_or_none() is None:
        db.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=admin.id,
                role=WorkspaceRole.OWNER,
            )
        )
        logger.info(
            "Added bootstrap admin as owner of primary workspace %s",
            workspace.slug,
        )
    return workspace
