"""Local (email/password) authentication service.

Only active when AUTH_MODE=local. Users authenticate directly against the
API, which mints session tokens (see stardag_api.auth.tokens); no external
identity provider is involved.
"""

import logging
import time
from collections import defaultdict, deque
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
from stardag_api.models import User

logger = logging.getLogger(__name__)


class LoginRateLimiter:
    """In-process sliding-window rate limiter for login attempts.

    Sufficient for single-container deployments (the self-hosted target for
    local auth mode); multi-container deployments rate-limit per container,
    which still bounds attack throughput per replica.
    """

    def __init__(self, max_attempts: int = 10, window_seconds: float = 300.0):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        """Record an attempt for key; returns False when rate-limited."""
        now = time.monotonic()
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if len(attempts) >= self.max_attempts:
            return False
        attempts.append(now)
        return True

    def reset(self, key: str) -> None:
        """Clear attempts for key (e.g. after successful login)."""
        self._attempts.pop(key, None)


login_rate_limiter = LoginRateLimiter()


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
        user.password_hash = await hash_password_async(password)
        await db.commit()
        logger.info("Bootstrap admin password set for existing user: %s", email)
    else:
        logger.debug("Bootstrap admin already provisioned: %s", email)
