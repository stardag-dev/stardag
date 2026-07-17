"""Tests for the self-host "complete setup": Modal-environment isolation
plumbing and the post-deploy connect flow (mocked Modal + mocked HTTP API).
"""

import json
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("modal")
pytest.importorskip("cryptography")

import typer  # noqa: E402

from stardag._cli._selfhost_connect import (  # noqa: E402
    API_KEY_SECRET_NAME,
    ConnectOutcome,
    parse_target_root_flag,
    resolve_primary_workspace,
    run_connect,
)
from stardag._cli.selfhost import (  # noqa: E402
    ModalWorkspaceInfo,
    _build_config_env,
    _ensure_jwt_secret,
    _push_secret,
    _resolve_keep_warm,
    _secret_exists,
)
from stardag.selfhost._modal_app import (  # noqa: E402
    DEFAULT_SERVER_MODAL_ENV,
    build_server_app,
)

API_URL = "http://stardag.test"


# ---------------------------------------------------------------------------
# Primary workspace derivation
# ---------------------------------------------------------------------------


def test_modal_workspace_info_display():
    shared = ModalWorkspaceInfo(username="alice", workspace_name="Acme Corp")
    personal = ModalWorkspaceInfo(username="alice", workspace_name=None)
    assert shared.display == "Acme Corp"
    assert personal.display == "alice"


def test_resolve_primary_workspace_shared_modal_workspace():
    # Non-interactive: the shared Modal workspace name is the default
    assert (
        resolve_primary_workspace(None, False, "Acme Corp", interactive=False)
        == "Acme Corp"
    )


def test_resolve_primary_workspace_personal_modal_workspace():
    # Personal Modal workspace: no shared Stardag workspace by default
    assert resolve_primary_workspace(None, False, None, interactive=False) is None


def test_resolve_primary_workspace_explicit_wins():
    assert (
        resolve_primary_workspace("My Team", False, "Acme Corp", interactive=False)
        == "My Team"
    )
    # Explicit also works for personal Modal workspaces
    assert (
        resolve_primary_workspace("My Team", False, None, interactive=False)
        == "My Team"
    )


def test_resolve_primary_workspace_opt_out():
    assert resolve_primary_workspace(None, True, "Acme Corp", interactive=False) is None


def test_resolve_primary_workspace_interactive_confirm(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    assert (
        resolve_primary_workspace(None, False, "Acme Corp", interactive=True)
        == "Acme Corp"
    )
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    assert resolve_primary_workspace(None, False, "Acme Corp", interactive=True) is None


def test_parse_target_root_flag():
    assert parse_target_root_flag("default=modalvol://stardag/acme") == (
        "default",
        "modalvol://stardag/acme",
    )
    with pytest.raises(typer.Exit):
        parse_target_root_flag("no-equals-sign")
    with pytest.raises(typer.Exit):
        parse_target_root_flag("=uri-only")


# ---------------------------------------------------------------------------
# Config secret assembly (primary workspace env vars)
# ---------------------------------------------------------------------------


def _config_env(auth_mode="local", **kwargs):
    return _build_config_env(
        pooled_url="postgresql+asyncpg://u:p@pooled/db",
        direct_url="postgresql+asyncpg://u:p@direct/db",
        pooler_compat=True,
        auth_mode=auth_mode,
        admin_email="admin@example.com" if auth_mode == "local" else None,
        admin_password="s3cret-pass" if auth_mode == "local" else None,
        enable_registration=False,
        oidc_issuer="https://idp.example.com" if auth_mode == "oidc" else None,
        oidc_sdk_client_id=None,
        oidc_ui_client_id=None,
        oidc_audience=None,
        oidc_jwks_url=None,
        **kwargs,
    )


def test_build_config_env_primary_workspace():
    env = _config_env(
        primary_workspace_name="Acme Corp",
        primary_workspace_environment="main",
    )
    assert env["AUTH_PRIMARY_WORKSPACE_NAME"] == "Acme Corp"
    assert env["AUTH_PRIMARY_WORKSPACE_ENVIRONMENT"] == "main"


def test_build_config_env_no_primary_workspace():
    # Personal Modal workspace: no name, but the primary environment is
    # still ensured (in the admin's personal workspace, server-side)
    env = _config_env(primary_workspace_environment="main")
    assert "AUTH_PRIMARY_WORKSPACE_NAME" not in env
    assert env["AUTH_PRIMARY_WORKSPACE_ENVIRONMENT"] == "main"


def test_build_config_env_defaults_omit_primary_vars():
    env = _config_env()
    assert "AUTH_PRIMARY_WORKSPACE_NAME" not in env
    assert "AUTH_PRIMARY_WORKSPACE_ENVIRONMENT" not in env


# ---------------------------------------------------------------------------
# Modal environment plumbing (server-side objects)
# ---------------------------------------------------------------------------


class _FakeSecretObjects:
    def __init__(self):
        self.calls: list[tuple] = []

    def delete(self, name, *, allow_missing=False, environment_name=None):
        self.calls.append(("delete", name, environment_name))

    def create(self, name, env_dict, *, environment_name=None, **kwargs):
        self.calls.append(("create", name, environment_name, env_dict))


@pytest.fixture
def fake_secret_objects(monkeypatch: pytest.MonkeyPatch) -> _FakeSecretObjects:
    import modal

    fake = _FakeSecretObjects()
    monkeypatch.setattr(modal.Secret, "objects", fake)
    return fake


def test_push_secret_passes_environment(fake_secret_objects: _FakeSecretObjects):
    _push_secret("my-secret", {"A": "1"}, "stardag-host")
    assert fake_secret_objects.calls == [
        ("delete", "my-secret", "stardag-host"),
        ("create", "my-secret", "stardag-host", {"A": "1"}),
    ]


def test_secret_exists_passes_environment(monkeypatch: pytest.MonkeyPatch):
    import modal

    seen: list[tuple] = []

    class FakeRef:
        def hydrate(self):
            raise modal.exception.NotFoundError("nope")

    def fake_from_name(name, *, environment_name=None, **kwargs):
        seen.append((name, environment_name))
        return FakeRef()

    monkeypatch.setattr(modal.Secret, "from_name", fake_from_name)
    assert _secret_exists("my-secret", "stardag-host") is False
    assert seen == [("my-secret", "stardag-host")]


def test_ensure_jwt_secret_creates_in_environment(
    monkeypatch: pytest.MonkeyPatch, fake_secret_objects: _FakeSecretObjects
):
    monkeypatch.setattr(
        "stardag._cli.selfhost._secret_exists", lambda name, env=None: False
    )
    assert _ensure_jwt_secret("app-jwt", "stardag-host") is True
    kinds = [(kind, name, env) for kind, name, env, *rest in fake_secret_objects.calls]
    assert ("create", "app-jwt", "stardag-host") in kinds


def test_resolve_keep_warm_passes_environment(monkeypatch: pytest.MonkeyPatch):
    import modal

    envs: list = []
    store: dict = {}

    def fake_from_name(name, *, create_if_missing=False, environment_name=None):
        envs.append(environment_name)
        return store

    monkeypatch.setattr(modal.Dict, "from_name", fake_from_name)
    assert _resolve_keep_warm("myapp", 1, "stardag-host") == 1
    assert envs == ["stardag-host"]


def test_build_server_app_resolves_secrets_in_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    import modal

    seen: list[tuple] = []
    real_from_name = modal.Secret.from_name

    def recording_from_name(name, *, environment_name=None, **kwargs):
        seen.append((name, environment_name))
        return real_from_name(name, environment_name=environment_name, **kwargs)

    monkeypatch.setattr(modal.Secret, "from_name", recording_from_name)
    build_server_app(server_version="1.2.3", environment_name=DEFAULT_SERVER_MODAL_ENV)
    # Secret names derive from the default app name ("server")
    assert seen == [
        ("server-config", DEFAULT_SERVER_MODAL_ENV),
        ("server-jwt", DEFAULT_SERVER_MODAL_ENV),
    ]


def test_default_names_avoid_doubled_url_label():
    """The Modal env and app name differ, so the default URL reads
    ...-stardag-host--server.modal.run rather than a doubled label."""
    from stardag.selfhost._modal_app import (
        DEFAULT_APP_NAME,
        DEFAULT_SERVER_MODAL_ENV,
    )

    assert DEFAULT_APP_NAME == "server"
    assert DEFAULT_SERVER_MODAL_ENV == "stardag-host"
    assert DEFAULT_APP_NAME != DEFAULT_SERVER_MODAL_ENV


def test_default_target_root_uri_volume_per_workspace_environment():
    from stardag._cli._selfhost_connect import default_target_root_uri

    # Both identifiers live in the *volume name*; the path is the root name.
    assert (
        default_target_root_uri("acme-corp", "main")
        == "modalvol://stardag-targets-acme-corp-main/default"
    )


def test_targets_volume_name_length_guard():
    import re

    from stardag._cli._selfhost_connect import _targets_volume_name

    # Volume-name charset per Modal (max 64 chars, [a-zA-Z0-9-_.]).
    charset = re.compile(r"^[a-zA-Z0-9._-]+$")

    # Short (ws, env): composed verbatim, no hashing.
    assert _targets_volume_name("acme-corp", "main") == "stardag-targets-acme-corp-main"

    long_ws = "w" * 60
    long_env = "e" * 40
    name = _targets_volume_name(long_ws, long_env)
    assert len(name) <= 64
    assert charset.match(name)

    # Deterministic: same inputs -> same name.
    assert name == _targets_volume_name(long_ws, long_env)

    # Distinct overflowing (ws, env) pairs -> distinct names (no collision).
    other = _targets_volume_name(long_ws, long_env + "x")
    assert len(other) <= 64
    assert other != name
    # A pair that composes to the same string under a naive "-".join but is a
    # genuinely different (ws, env) split must still differ.
    assert _targets_volume_name("a" * 40 + "-b", "c" * 40) != _targets_volume_name(
        "a" * 40, "b-" + "c" * 40
    )


# ---------------------------------------------------------------------------
# Connect flow (mocked registry API)
# ---------------------------------------------------------------------------


class FakeRegistryApi:
    """Minimal in-memory fake of the registry endpoints the connect flow uses."""

    def __init__(self, user_email="admin@example.com", workspaces=None):
        self.user_id = str(uuid4())
        self.user_email = user_email
        # workspace: {id, name, slug, is_personal, envs: {slug: {...}}}
        self.workspaces = workspaces if workspaces is not None else []
        self.api_key_requests: list[dict] = []
        self.requests: list[str] = []
        self.fail_api_key_create = False

    def _find_workspace(self, workspace_id: str) -> dict:
        return next(ws for ws in self.workspaces if ws["id"] == workspace_id)

    def add_workspace(self, name, slug, is_personal=False, env_slugs=()):
        ws = {
            "id": str(uuid4()),
            "name": name,
            "slug": slug,
            "is_personal": is_personal,
            "envs": {
                s: {
                    "id": str(uuid4()),
                    "slug": s,
                    "name": s,
                    "target_roots": [],
                    "api_keys": [],
                }
                for s in env_slugs
            },
        }
        self.workspaces.append(ws)
        return ws

    def add_api_key(self, env: dict, name: str, revoked_at=None) -> dict:
        key = {
            "id": str(uuid4()),
            "environment_id": env["id"],
            "name": name,
            "key_prefix": f"sk_old{len(env['api_keys'])}",
            "created_by_id": self.user_id,
            "created_at": "2026-01-01T00:00:00",
            "last_used_at": None,
            "revoked_at": revoked_at,
        }
        env["api_keys"].append(key)
        return key

    def live_keys(self, env: dict, name: str) -> list[dict]:
        return [
            k for k in env["api_keys"] if k["name"] == name and k["revoked_at"] is None
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.requests.append(f"{method} {path}")

        if path == "/api/v1/ui/me" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "user": {
                        "id": self.user_id,
                        "external_id": "local:x",
                        "email": self.user_email,
                        "display_name": None,
                    },
                    "workspaces": [
                        {
                            "id": ws["id"],
                            "name": ws["name"],
                            "slug": ws["slug"],
                            "role": "owner",
                            "is_personal": ws["is_personal"],
                        }
                        for ws in self.workspaces
                    ],
                },
            )

        if path == "/api/v1/ui/workspaces" and method == "POST":
            data = json.loads(request.content)
            if any(ws["slug"] == data["slug"] for ws in self.workspaces):
                return httpx.Response(409, json={"detail": "slug exists"})
            env_slug = data.get("initial_environment_slug") or "default"
            ws = self.add_workspace(data["name"], data["slug"], env_slugs=(env_slug,))
            return httpx.Response(
                201,
                json={
                    "id": ws["id"],
                    "name": ws["name"],
                    "slug": ws["slug"],
                    "description": None,
                    "is_personal": False,
                },
            )

        if path == "/api/v1/auth/exchange" and method == "POST":
            data = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "access_token": f"ws-token-{data['workspace_id']}",
                    "expires_in": 600,
                },
            )

        parts = path.strip("/").split("/")
        # /api/v1/ui/workspaces/{ws}/environments[...]
        if len(parts) >= 6 and parts[3] == "workspaces" and parts[5] == "environments":
            ws = self._find_workspace(parts[4])
            if len(parts) == 6:
                if method == "GET":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "id": env["id"],
                                "workspace_id": ws["id"],
                                "name": env["name"],
                                "slug": env["slug"],
                                "description": None,
                            }
                            for env in ws["envs"].values()
                        ],
                    )
                if method == "POST":
                    data = json.loads(request.content)
                    env = {
                        "id": str(uuid4()),
                        "slug": data["slug"],
                        "name": data["name"],
                        "target_roots": [],
                    }
                    ws["envs"][data["slug"]] = env
                    return httpx.Response(
                        201,
                        json={
                            "id": env["id"],
                            "workspace_id": ws["id"],
                            "name": env["name"],
                            "slug": env["slug"],
                            "description": None,
                        },
                    )
            env = next(e for e in ws["envs"].values() if e["id"] == parts[6])
            if len(parts) == 8 and parts[7] == "target-roots":
                if method == "GET":
                    return httpx.Response(200, json=env["target_roots"])
                if method == "POST":
                    data = json.loads(request.content)
                    root = {
                        "id": str(uuid4()),
                        "environment_id": env["id"],
                        "name": data["name"],
                        "uri_prefix": data["uri_prefix"],
                        "created_at": "2026-01-01T00:00:00",
                    }
                    env["target_roots"].append(root)
                    return httpx.Response(201, json=root)
            if len(parts) == 8 and parts[7] == "api-keys":
                if method == "GET":
                    return httpx.Response(200, json=env["api_keys"])
                if method == "POST":
                    if self.fail_api_key_create:
                        return httpx.Response(500, json={"detail": "boom"})
                    data = json.loads(request.content)
                    self.api_key_requests.append(
                        {"environment_id": env["id"], "name": data["name"]}
                    )
                    key = self.add_api_key(env, data["name"])
                    key["key_prefix"] = "sk_test1234"
                    return httpx.Response(
                        201, json={**key, "key": "sk_test1234_full_key_value"}
                    )
            if len(parts) == 9 and parts[7] == "api-keys" and method == "DELETE":
                key = next(k for k in env["api_keys"] if k["id"] == parts[8])
                key["revoked_at"] = "2026-01-02T00:00:00"
                return httpx.Response(204)

        raise AssertionError(f"Unexpected request: {method} {path}")


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Point ~ (and the stardag config dir) at a temp directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("STARDAG_PROFILE", raising=False)
    from stardag.config.loader import clear_config_cache

    clear_config_cache()
    yield tmp_path
    clear_config_cache()


class _SecretPushRecorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, secret_name, env_dict, modal_env):
        self.calls.append((secret_name, env_dict, modal_env))


def _run_connect(
    api: FakeRegistryApi, **kwargs
) -> tuple[ConnectOutcome, "_SecretPushRecorder"]:
    push = _SecretPushRecorder()
    outcome = run_connect(
        API_URL,
        "session-token",
        api.user_email,
        push_modal_secret=push,
        transport=httpx.MockTransport(api.handler),
        **kwargs,
    )
    return outcome, push


def test_connect_personal_workspace(isolated_home):
    """Personal Modal workspace: personal Stardag workspace + main env
    (pre-created server-side), API key pushed to the default Modal env."""
    api = FakeRegistryApi()
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("local", "main")
    )

    outcome, push = _run_connect(api, primary_workspace=None)

    assert outcome.workspace_slug == "admin"
    assert outcome.workspace_is_personal is True
    assert outcome.workspace_created is False
    assert outcome.environment_slug == "main"
    assert outcome.environment_created is False
    assert outcome.target_root == (
        "default",
        "modalvol://stardag-targets-admin-main/default",
    )
    assert outcome.api_key_name == "modal-default"
    assert outcome.modal_secret_name == API_KEY_SECRET_NAME

    # API key pushed as Modal secret into the default (None) Modal env
    assert len(push.calls) == 1
    secret_name, env_dict, modal_env = push.calls[0]
    assert secret_name == API_KEY_SECRET_NAME
    assert env_dict == {"STARDAG_API_KEY": "sk_test1234_full_key_value"}
    assert modal_env is None

    # Local SDK config written
    from stardag._cli.credentials import list_profiles, list_registries

    assert list_registries() == {"selfhosted": API_URL}
    profiles = list_profiles()
    assert profiles["selfhosted"] == {
        "registry": "selfhosted",
        "user": "admin@example.com",
        "workspace": "admin",
        "environment": "main",
    }


def test_connect_creates_primary_workspace(isolated_home):
    """Shared Modal workspace: the primary Stardag workspace + main env are
    created via the API when missing (e.g. OIDC mode or --skip-connect)."""
    api = FakeRegistryApi()
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("local",)
    )

    outcome, push = _run_connect(
        api, primary_workspace="Acme Corp", execution_modal_env="staging"
    )

    assert outcome.workspace_name == "Acme Corp"
    assert outcome.workspace_slug == "acme-corp"
    assert outcome.workspace_created is True
    assert outcome.workspace_is_personal is False
    # main env was included in workspace creation, so not created separately
    assert outcome.environment_created is False
    assert outcome.target_root == (
        "default",
        "modalvol://stardag-targets-acme-corp-main/default",
    )
    assert outcome.api_key_name == "modal-staging"
    assert push.calls[0][2] == "staging"
    assert api.api_key_requests[0]["name"] == "modal-staging"


def test_connect_existing_primary_workspace_idempotent(isolated_home):
    """Re-running connect matches the existing workspace/env/target root."""
    api = FakeRegistryApi()
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("local",)
    )
    ws = api.add_workspace("Acme Corp", "acme-corp", env_slugs=("main",))
    env = ws["envs"]["main"]
    env["target_roots"].append(
        {
            "id": str(uuid4()),
            "environment_id": env["id"],
            "name": "default",
            "uri_prefix": "modalvol://custom/acme",
            "created_at": "2026-01-01T00:00:00",
        }
    )

    outcome, _ = _run_connect(api, primary_workspace="Acme Corp")

    assert outcome.workspace_created is False
    assert outcome.environment_created is False
    # Existing target root is kept, not overwritten
    assert outcome.target_root == ("default", "modalvol://custom/acme")
    assert "POST /api/v1/ui/workspaces" not in api.requests


def test_connect_creates_missing_environment(isolated_home):
    """The primary env is created when absent (e.g. pre-existing workspace)."""
    api = FakeRegistryApi()
    api.add_workspace("Acme Corp", "acme-corp", env_slugs=("default",))
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("local",)
    )

    outcome, _ = _run_connect(api, primary_workspace="Acme Corp")

    assert outcome.environment_created is True
    assert outcome.environment_slug == "main"


def test_connect_target_root_override_and_skip(isolated_home):
    api = FakeRegistryApi()
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("main",)
    )
    outcome, _ = _run_connect(
        api,
        primary_workspace=None,
        target_root=("blobs", "modalvol://myvol/data"),
    )
    assert outcome.target_root == ("blobs", "modalvol://myvol/data")

    api2 = FakeRegistryApi()
    api2.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("main",)
    )
    outcome2, _ = _run_connect(api2, primary_workspace=None, no_target_root=True)
    assert outcome2.target_root is None
    assert not any("target-roots" in r for r in api2.requests)


def test_connect_rotates_existing_api_key(isolated_home):
    """Re-running connect revokes the previous same-named key before
    minting a new one - no live-credential accumulation."""
    api = FakeRegistryApi()
    ws = api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("main",)
    )
    env = ws["envs"]["main"]
    api.add_api_key(env, "modal-default")
    # An already-revoked key and a differently-named key are left alone
    api.add_api_key(env, "modal-default", revoked_at="2025-01-01T00:00:00")
    api.add_api_key(env, "other-key")

    outcome, push = _run_connect(api, primary_workspace=None)

    assert outcome.api_key_name == "modal-default"
    assert len(api.live_keys(env, "modal-default")) == 1
    assert api.live_keys(env, "modal-default")[0]["key_prefix"] == "sk_test1234"
    assert len(api.live_keys(env, "other-key")) == 1
    assert len(push.calls) == 1

    # Second run: still exactly one live key with that name
    _run_connect(api, primary_workspace=None)
    assert len(api.live_keys(env, "modal-default")) == 1


def test_connect_api_key_creation_failure_is_not_fatal(isolated_home):
    """API-key creation failures degrade gracefully (connect-specific
    handling, no typer.Exit): no secret pushed, SDK config still written."""
    api = FakeRegistryApi()
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("main",)
    )
    api.fail_api_key_create = True

    outcome, push = _run_connect(api, primary_workspace=None)

    assert outcome.api_key_name is None
    assert outcome.modal_secret_name is None
    assert push.calls == []

    # The rest of the setup completed: profile + registry written
    from stardag._cli.credentials import list_profiles, list_registries

    assert list_registries() == {"selfhosted": API_URL}
    assert "selfhosted" in list_profiles()


def test_connect_modal_secret_push_failure_is_not_fatal(isolated_home):
    """A failed Modal secret push degrades gracefully: config still written."""
    api = FakeRegistryApi()
    api.add_workspace(
        "Admin's Workspace", "admin", is_personal=True, env_slugs=("main",)
    )

    def failing_push(*args):
        raise RuntimeError("modal down")

    outcome = run_connect(
        API_URL,
        "session-token",
        api.user_email,
        primary_workspace=None,
        push_modal_secret=failing_push,
        transport=httpx.MockTransport(api.handler),
    )
    assert outcome.api_key_name is None
    assert outcome.modal_secret_name is None

    from stardag._cli.credentials import list_registries

    assert list_registries() == {"selfhosted": API_URL}
