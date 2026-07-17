"""Password hashing for local auth mode (bcrypt, mirrors api_keys hashing)."""

import asyncio

import bcrypt

# Verified against when a login targets an unknown email, so both code paths
# cost one bcrypt verification and response timing doesn't reveal whether the
# account exists.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt()).decode("utf-8")

MIN_PASSWORD_LENGTH = 8
# bcrypt truncates input at 72 bytes; reject longer instead of silently
# truncating.
MAX_PASSWORD_LENGTH = 72


class PasswordPolicyError(ValueError):
    """Password does not meet policy requirements."""


def validate_password_policy(password: str) -> None:
    """Validate password against policy; raises PasswordPolicyError."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} bytes"
        )


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password against a stored hash.

    Accepts None for the hash (user unknown, or OIDC-only user without a
    password) and still performs a bcrypt verification against a dummy hash
    so timing is uniform.
    """
    effective_hash = password_hash or _DUMMY_HASH
    try:
        matches = bcrypt.checkpw(
            password.encode("utf-8"), effective_hash.encode("utf-8")
        )
    except ValueError:
        return False
    return matches and password_hash is not None


async def verify_password_async(password: str, password_hash: str | None) -> bool:
    """Async wrapper: bcrypt is CPU-bound; don't block the event loop."""
    return await asyncio.to_thread(verify_password, password, password_hash)


async def hash_password_async(password: str) -> str:
    """Async wrapper: bcrypt is CPU-bound; don't block the event loop."""
    return await asyncio.to_thread(hash_password, password)
