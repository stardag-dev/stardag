"""Tests for local (email/password) auth mode."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.passwords import hash_password, verify_password
from stardag_api.auth.tokens import (
    TokenInvalidError,
    get_token_manager,
)
from stardag_api.config import auth_settings
from stardag_api.services.local_auth import (
    LoginRateLimiter,
    create_local_user,
    ensure_bootstrap_admin,
    login_email_rate_limiter,
    login_rate_limiter,
)


@pytest.fixture(autouse=True)
def local_mode(monkeypatch: pytest.MonkeyPatch):
    """Run these tests in local auth mode with clean rate limiters."""
    monkeypatch.setattr(auth_settings, "mode", "local")
    login_rate_limiter._attempts.clear()
    login_email_rate_limiter._attempts.clear()
    yield
    login_rate_limiter._attempts.clear()
    login_email_rate_limiter._attempts.clear()


@pytest.fixture
def registration_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth_settings, "local_registration_enabled", True)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_verify_password_none_hash_uniform():
    # Unknown user / OIDC-only user: must return False, not raise
    assert not verify_password("anything", None)


# ---------------------------------------------------------------------------
# Token manager: session vs workspace token separation
# ---------------------------------------------------------------------------


def test_session_token_rejected_as_workspace_token():
    from datetime import timedelta

    manager = get_token_manager()
    session_token = manager.create_session_token("user-1", timedelta(hours=1))
    with pytest.raises(TokenInvalidError):
        manager.validate_token(session_token)


def test_workspace_token_rejected_as_session_token():
    manager = get_token_manager()
    access_token = manager.create_access_token("user-1", "ws-1")
    with pytest.raises(TokenInvalidError):
        manager.validate_session_token(access_token)


def test_session_token_roundtrip():
    from datetime import timedelta

    manager = get_token_manager()
    token = manager.create_session_token("user-42", timedelta(hours=1))
    payload = manager.validate_session_token(token)
    assert payload.sub == "user-42"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_blocks_after_max():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    assert limiter.check("k")
    assert limiter.check("k")
    assert limiter.check("k")
    assert not limiter.check("k")
    limiter.reset("k")
    assert limiter.check("k")


def test_rate_limiter_expired_window_unblocks(monkeypatch: pytest.MonkeyPatch):
    current = {"t": 1000.0}
    monkeypatch.setattr(
        "stardag_api.services.local_auth.time.monotonic", lambda: current["t"]
    )
    limiter = LoginRateLimiter(max_attempts=1, window_seconds=60)
    assert limiter.check("k")
    assert not limiter.check("k")
    current["t"] += 61
    assert limiter.check("k")  # window expired: allowed again
    limiter.reset("k")
    assert "k" not in limiter._attempts


def test_rate_limiter_sweeps_expired_keys(monkeypatch: pytest.MonkeyPatch):
    """Keys with fully-expired windows are dropped, bounding memory even for
    attacker-chosen (unauthenticated) keys."""
    current = {"t": 1000.0}
    monkeypatch.setattr(
        "stardag_api.services.local_auth.time.monotonic", lambda: current["t"]
    )
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60, sweep_threshold=5)
    for i in range(6):
        assert limiter.check(f"key-{i}")
    assert len(limiter._attempts) == 6
    # Below/at the threshold nothing is swept eagerly
    current["t"] += 61
    assert limiter.check("fresh")  # exceeds threshold -> sweep expired keys
    assert set(limiter._attempts) == {"fresh"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _register(client: AsyncClient, email: str, password: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": "Test User"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_register_login_exchange_flow(
    unauthenticated_client: AsyncClient, registration_enabled
):
    """Full local-auth flow: register -> /ui/me -> exchange -> authed call."""
    data = await _register(unauthenticated_client, "flow@example.com", "s3cret-pass")
    assert data["token_type"] == "Bearer"
    session_token = data["session_token"]

    # Session token works for bootstrap endpoint
    me = await unauthenticated_client.get(
        "/api/v1/ui/me", headers={"Authorization": f"Bearer {session_token}"}
    )
    assert me.status_code == 200, me.text
    me_data = me.json()
    assert me_data["user"]["email"] == "flow@example.com"
    assert len(me_data["workspaces"]) == 1  # personal workspace auto-created
    workspace_id = me_data["workspaces"][0]["id"]

    # Exchange session token for workspace-scoped token
    exchange = await unauthenticated_client.post(
        "/api/v1/auth/exchange",
        json={"workspace_id": workspace_id},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert exchange.status_code == 200, exchange.text
    access_token = exchange.json()["access_token"]

    # Workspace token works on bootstrap endpoint too (flexible auth)
    me2 = await unauthenticated_client.get(
        "/api/v1/ui/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me2.status_code == 200

    # Fresh login with the registered credentials
    login = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "flow@example.com", "password": "s3cret-pass"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["session_token"]


@pytest.mark.asyncio
async def test_login_wrong_password(
    unauthenticated_client: AsyncClient, registration_enabled
):
    await _register(unauthenticated_client, "wrongpw@example.com", "s3cret-pass")
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "wrongpw@example.com", "password": "not-the-password"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email(unauthenticated_client: AsyncClient):
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "whatever-pass"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_rejected_in_oidc_mode(
    unauthenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(auth_settings, "mode", "oidc")
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.com", "password": "whatever-pass"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_session_token_rejected_in_oidc_mode(
    unauthenticated_client: AsyncClient,
    registration_enabled,
    monkeypatch: pytest.MonkeyPatch,
):
    """Session tokens minted in local mode must not authenticate after a
    deployment switches to OIDC mode (same JWT keys)."""
    data = await _register(unauthenticated_client, "switch@example.com", "s3cret-pass")
    session_token = data["session_token"]
    headers = {"Authorization": f"Bearer {session_token}"}

    # Sanity check: works in local mode
    me = await unauthenticated_client.get("/api/v1/ui/me", headers=headers)
    assert me.status_code == 200

    monkeypatch.setattr(auth_settings, "mode", "oidc")

    me = await unauthenticated_client.get("/api/v1/ui/me", headers=headers)
    assert me.status_code == 401

    exchange = await unauthenticated_client.post(
        "/api/v1/auth/exchange",
        json={"workspace_id": "00000000-0000-0000-0000-000000000000"},
        headers=headers,
    )
    assert exchange.status_code == 401


@pytest.mark.asyncio
async def test_register_disabled_by_default(unauthenticated_client: AsyncClient):
    response = await unauthenticated_client.post(
        "/api/v1/auth/register",
        json={"email": "nope@example.com", "password": "s3cret-pass"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_register_weak_password(
    unauthenticated_client: AsyncClient, registration_enabled
):
    response = await unauthenticated_client.post(
        "/api/v1/auth/register",
        json={"email": "weak@example.com", "password": "short"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_register_duplicate_email(
    unauthenticated_client: AsyncClient, registration_enabled
):
    await _register(unauthenticated_client, "dupe@example.com", "s3cret-pass")
    response = await unauthenticated_client.post(
        "/api/v1/auth/register",
        json={"email": "dupe@example.com", "password": "0ther-pass"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_login_rate_limited(
    unauthenticated_client: AsyncClient,
):
    for _ in range(10):
        response = await unauthenticated_client.post(
            "/api/v1/auth/login",
            json={"email": "brute@example.com", "password": "guess-a-pass"},
        )
        assert response.status_code == 401
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "brute@example.com", "password": "guess-a-pass"},
    )
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_login_rate_limit_ignores_spoofed_xff_prefix(
    unauthenticated_client: AsyncClient,
):
    """The rate-limit IP is the LAST X-Forwarded-For entry (appended by the
    nearest trusted proxy); rotating the client-supplied prefix must not
    yield a fresh rate-limit bucket per request."""
    for i in range(10):
        response = await unauthenticated_client.post(
            "/api/v1/auth/login",
            json={"email": "xff@example.com", "password": "guess-a-pass"},
            headers={"x-forwarded-for": f"10.0.0.{i}, 203.0.113.9"},
        )
        assert response.status_code == 401
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "xff@example.com", "password": "guess-a-pass"},
        headers={"x-forwarded-for": "10.0.99.99, 203.0.113.9"},
    )
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_login_per_email_limiter_blocks_ip_rotation(
    unauthenticated_client: AsyncClient,
):
    """Even with a fully attacker-controlled (rotating) client IP, the
    per-email limiter caps total attempts against one account."""
    for i in range(30):
        response = await unauthenticated_client.post(
            "/api/v1/auth/login",
            json={"email": "rotated@example.com", "password": "guess-a-pass"},
            headers={"x-forwarded-for": f"10.0.{i}.1, 198.51.100.{i}"},
        )
        assert response.status_code == 401
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "rotated@example.com", "password": "guess-a-pass"},
        headers={"x-forwarded-for": "10.0.255.1, 198.51.100.255"},
    )
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_login_success_resets_both_limiters(
    unauthenticated_client: AsyncClient, registration_enabled
):
    await _register(unauthenticated_client, "resetme@example.com", "s3cret-pass")
    for _ in range(5):
        response = await unauthenticated_client.post(
            "/api/v1/auth/login",
            json={"email": "resetme@example.com", "password": "wrong-pass"},
        )
        assert response.status_code == 401
    response = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "resetme@example.com", "password": "s3cret-pass"},
    )
    assert response.status_code == 200
    assert not any("resetme@example.com" in k for k in login_rate_limiter._attempts)
    assert "resetme@example.com" not in login_email_rate_limiter._attempts


@pytest.mark.asyncio
async def test_change_password_flow(
    unauthenticated_client: AsyncClient, registration_enabled
):
    data = await _register(
        unauthenticated_client, "changer@example.com", "0ld-passw0rd"
    )
    session_token = data["session_token"]
    headers = {"Authorization": f"Bearer {session_token}"}

    # Wrong current password
    response = await unauthenticated_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "not-it", "new_password": "n3w-passw0rd"},
        headers=headers,
    )
    assert response.status_code == 401

    # Correct current password
    response = await unauthenticated_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "0ld-passw0rd", "new_password": "n3w-passw0rd"},
        headers=headers,
    )
    assert response.status_code == 204

    # Old password no longer works, new one does
    old_login = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "changer@example.com", "password": "0ld-passw0rd"},
    )
    assert old_login.status_code == 401
    new_login = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "changer@example.com", "password": "n3w-passw0rd"},
    )
    assert new_login.status_code == 200


def test_session_invalidated_by_password_change_helper():
    from datetime import datetime, timezone

    from stardag_api.auth.dependencies import session_invalidated_by_password_change
    from stardag_api.models import User

    user = User(external_id="x", email="helper@example.com")
    # No password change recorded: any iat is fine (OIDC users, fresh users)
    assert not session_invalidated_by_password_change(user, 0)

    changed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts = int(changed_at.timestamp())
    user.password_changed_at = changed_at
    assert session_invalidated_by_password_change(user, ts - 1)
    assert not session_invalidated_by_password_change(user, ts)
    assert not session_invalidated_by_password_change(user, ts + 1)

    # Naive datetimes (e.g. read back from SQLite) are interpreted as UTC
    user.password_changed_at = datetime(2026, 1, 1)
    assert session_invalidated_by_password_change(user, ts - 1)
    assert not session_invalidated_by_password_change(user, ts)


@pytest.mark.asyncio
async def test_password_change_invalidates_old_session_tokens(
    unauthenticated_client: AsyncClient, registration_enabled
):
    """A session token minted before a password change is rejected afterwards
    (both on flexible-auth endpoints and on /auth/exchange); a token minted
    after the change works; workspace tokens are unaffected."""
    import time as time_module

    data = await _register(
        unauthenticated_client, "invalidate@example.com", "0ld-passw0rd"
    )
    old_token = data["session_token"]
    old_headers = {"Authorization": f"Bearer {old_token}"}

    me = await unauthenticated_client.get("/api/v1/ui/me", headers=old_headers)
    assert me.status_code == 200
    workspace_id = me.json()["workspaces"][0]["id"]

    # Mint a workspace token before the change
    exchange = await unauthenticated_client.post(
        "/api/v1/auth/exchange",
        json={"workspace_id": workspace_id},
        headers=old_headers,
    )
    assert exchange.status_code == 200
    workspace_token = exchange.json()["access_token"]

    # Ensure the change lands in a later whole second than the token's iat
    # (the cutoff is compared at JWT iat granularity)
    time_module.sleep(1.1)

    response = await unauthenticated_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "0ld-passw0rd", "new_password": "n3w-passw0rd"},
        headers=old_headers,
    )
    assert response.status_code == 204

    # Old session token is now rejected on both session-token surfaces
    me = await unauthenticated_client.get("/api/v1/ui/me", headers=old_headers)
    assert me.status_code == 401
    exchange = await unauthenticated_client.post(
        "/api/v1/auth/exchange",
        json={"workspace_id": workspace_id},
        headers=old_headers,
    )
    assert exchange.status_code == 401

    # Workspace token minted before the change still works (10-min TTL)
    me = await unauthenticated_client.get(
        "/api/v1/ui/me", headers={"Authorization": f"Bearer {workspace_token}"}
    )
    assert me.status_code == 200

    # A session token minted after the change works
    login = await unauthenticated_client.post(
        "/api/v1/auth/login",
        json={"email": "invalidate@example.com", "password": "n3w-passw0rd"},
    )
    assert login.status_code == 200
    new_headers = {"Authorization": f"Bearer {login.json()['session_token']}"}
    me = await unauthenticated_client.get("/api/v1/ui/me", headers=new_headers)
    assert me.status_code == 200
    exchange = await unauthenticated_client.post(
        "/api/v1/auth/exchange",
        json={"workspace_id": workspace_id},
        headers=new_headers,
    )
    assert exchange.status_code == 200


# ---------------------------------------------------------------------------
# Bootstrap admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_admin_idempotent(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(auth_settings, "bootstrap_admin_email", "admin@example.com")
    monkeypatch.setattr(auth_settings, "bootstrap_admin_password", "adm1n-passw0rd")

    await ensure_bootstrap_admin(async_session)
    from sqlalchemy import select

    from stardag_api.models import User, WorkspaceMember

    result = await async_session.execute(
        select(User).where(User.email == "admin@example.com")
    )
    user = result.scalar_one()
    assert user.password_hash is not None
    original_hash = user.password_hash

    memberships = await async_session.execute(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
    )
    assert len(memberships.scalars().all()) == 1  # personal workspace

    # Second run: no-op, password not overwritten (even if setting changed)
    monkeypatch.setattr(auth_settings, "bootstrap_admin_password", "different-pass")
    await ensure_bootstrap_admin(async_session)
    await async_session.refresh(user)
    assert user.password_hash == original_hash


@pytest.mark.asyncio
async def test_bootstrap_admin_existing_user_password_policy(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Setting the bootstrap password on an existing passwordless user
    enforces the same policy as registration."""
    from stardag_api.auth.passwords import PasswordPolicyError
    from stardag_api.models import User

    user = User(external_id="ext-pre-created", email="pre@example.com")
    async_session.add(user)
    await async_session.flush()

    monkeypatch.setattr(auth_settings, "bootstrap_admin_email", "pre@example.com")
    monkeypatch.setattr(auth_settings, "bootstrap_admin_password", "short")
    with pytest.raises(PasswordPolicyError):
        await ensure_bootstrap_admin(async_session)

    await async_session.refresh(user)
    assert user.password_hash is None


@pytest.mark.asyncio
async def test_bootstrap_admin_noop_when_unset(async_session: AsyncSession):
    # Defaults: no bootstrap admin configured -> no error, no user
    await ensure_bootstrap_admin(async_session)


@pytest.mark.asyncio
async def test_create_local_user_password_policy(async_session: AsyncSession):
    from stardag_api.auth.passwords import PasswordPolicyError

    with pytest.raises(PasswordPolicyError):
        await create_local_user(async_session, email="p@example.com", password="short")
