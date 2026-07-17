"""CLI for self-hosting the Stardag service (API + UI) on Modal.

Usage:
    stardag self-host up          # provision + deploy + connect (complete setup)
    stardag self-host connect     # (re)run the post-deploy setup only
    stardag self-host upgrade     # migrate DB + redeploy
    stardag self-host status      # show deployment status and URL
    stardag self-host destroy     # stop the Modal app (DB is left untouched)

Requires the `selfhost` extra: pip install "stardag[selfhost]"

By default the prebuilt public server image is deployed (no repo checkout
needed). Pass --from-source to build from a local checkout of the stardag
repo instead (run from the checkout or pass --repo).

The server app and its secrets live in a dedicated Modal environment
(default: "stardag-server", created on demand) so they stay isolated from
the Modal environments where your DAG apps run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from stardag._cli._selfhost_connect import (
    PRIMARY_ENVIRONMENT_SLUG,
    get_auth_config,
    login_local,
    parse_target_root_flag,
    print_summary,
    resolve_primary_workspace,
    run_connect,
)
from stardag.selfhost._modal_app import (
    DEFAULT_APP_NAME,
    DEFAULT_SERVER_MODAL_ENV,
    DEFAULT_SERVER_VERSION,
    build_server_app,
    find_repo_root,
    server_image_ref,
)
from stardag.selfhost._neon import (
    NeonAuthError,
    NeonClient,
    NeonError,
    to_sqlalchemy_asyncpg_url,
)

app = typer.Typer(
    name="self-host",
    help="Self-host the Stardag service (Registry API + UI) on Modal.",
    no_args_is_help=True,
)

console = Console()
error_console = Console(stderr=True, style="bold red")

DEFAULT_NEON_PROJECT = "stardag"

# Mirrors the server's password policy (stardag_api.auth.passwords): bcrypt
# truncates at 72 bytes, so the server rejects longer passwords. Enforce the
# same bounds here - a too-long bootstrap password would otherwise make
# `ensure_bootstrap_admin` raise during API startup on every container boot.
MIN_ADMIN_PASSWORD_CHARS = 8
MAX_ADMIN_PASSWORD_BYTES = 72


# --- helpers ---------------------------------------------------------------


def _admin_password_error(password: str) -> str | None:
    """Validate the bootstrap admin password; returns an error message or None."""
    if len(password) < MIN_ADMIN_PASSWORD_CHARS:
        return f"Admin password must be at least {MIN_ADMIN_PASSWORD_CHARS} characters"
    if len(password.encode("utf-8")) > MAX_ADMIN_PASSWORD_BYTES:
        return (
            f"Admin password must be at most {MAX_ADMIN_PASSWORD_BYTES} bytes "
            "of UTF-8 (bcrypt limit, enforced by the server)"
        )
    return None


def _require_repo_root(repo: Path | None) -> Path:
    root = find_repo_root(repo)
    if root is None:
        error_console.print(
            "Could not locate a stardag repo checkout (looked for "
            "app/stardag-api and app/stardag-ui). --from-source requires a "
            "clone of https://github.com/stardag-dev/stardag: run this "
            "command from the clone or pass --repo. (Or drop --from-source "
            "to deploy the prebuilt server image - no checkout needed.)"
        )
        raise typer.Exit(1)
    return root


def _resolve_image_source(
    repo: Path | None,
    from_source: bool,
    server_version: str | None,
) -> tuple[Path | None, str | None]:
    """Resolve (repo_root, server_version) - exactly one is non-None.

    Default: prebuilt image at the given (or default) server version.
    --from-source (or an explicit --repo) builds from a local checkout;
    combining it with --server-version is an error.
    """
    build_from_source = from_source or repo is not None
    if build_from_source and server_version is not None:
        error_console.print(
            "--server-version only applies to prebuilt images and cannot be "
            "combined with --from-source/--repo."
        )
        raise typer.Exit(1)
    if build_from_source:
        return _require_repo_root(repo), None
    return None, server_version or DEFAULT_SERVER_VERSION


@dataclass
class ModalWorkspaceInfo:
    """The authenticated Modal account's workspace identity.

    ``workspace_name`` is the shared workspace's display name and is None
    for personal Modal workspaces; ``username`` is the account slug (always
    present).
    """

    username: str
    workspace_name: str | None

    @property
    def display(self) -> str:
        return self.workspace_name or self.username


def _check_modal_auth() -> ModalWorkspaceInfo:
    """Verify Modal credentials; returns the workspace identity."""
    import asyncio

    import modal.config

    try:
        config = modal.config.config
        token_id = config.get("token_id")
        token_secret = config.get("token_secret")
        server_url = config.get("server_url")
        if not token_id or not token_secret:
            raise ValueError("No Modal token configured")
        workspace = asyncio.run(
            modal.config._lookup_workspace(server_url, token_id, token_secret)
        )
        # workspace_name is empty for personal Modal workspaces
        return ModalWorkspaceInfo(
            username=workspace.username,
            workspace_name=workspace.workspace_name or None,
        )
    except Exception as e:
        error_console.print(f"Modal authentication not set up: {e}")
        console.print(
            "\nRun [bold]modal token new[/bold] (or [bold]uv run modal token new[/bold]) "
            "to authenticate with Modal first. Create a free account at "
            "https://modal.com if you don't have one."
        )
        raise typer.Exit(1)


def _ensure_modal_environment(name: str) -> bool:
    """Create the Modal environment if it doesn't exist yet.

    Returns True if it was created. The dedicated environment isolates the
    server app + secrets from the environments where user DAG apps run.
    """
    import modal.environments

    existing = {env.name for env in modal.environments.list_environments()}
    if name in existing:
        return False
    modal.environments.create_environment(name)
    return True


def _secret_exists(name: str, environment_name: str | None = None) -> bool:
    import modal
    import modal.exception

    try:
        modal.Secret.from_name(name, environment_name=environment_name).hydrate()
        return True
    except modal.exception.NotFoundError:
        return False


def _push_secret(
    name: str, env: dict[str, str], environment_name: str | None = None
) -> None:
    """Create or replace a named Modal secret."""
    import modal

    modal.Secret.objects.delete(
        name, allow_missing=True, environment_name=environment_name
    )
    modal.Secret.objects.create(name, env, environment_name=environment_name)


def _generate_jwt_keypair() -> tuple[str, str]:
    """Generate an RSA keypair (private_pem, public_pem) for internal JWTs."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _meta_dict_name(app_name: str) -> str:
    """Name of the modal.Dict holding persisted deployment settings."""
    return f"{app_name}-meta"


def _resolve_keep_warm(
    app_name: str, keep_warm: int | None, environment_name: str | None = None
) -> int:
    """Resolve the effective keep-warm value, persisting it across deploys.

    An explicitly provided value wins and is stored in the app's meta Dict;
    when the flag is omitted the previously stored value is used (default 0),
    so a plain `upgrade` doesn't silently reset keep-warm to scale-to-zero.
    """
    import modal

    meta = modal.Dict.from_name(
        _meta_dict_name(app_name),
        create_if_missing=True,
        environment_name=environment_name,
    )
    if keep_warm is not None:
        meta["keep_warm"] = keep_warm
        return keep_warm
    return meta.get("keep_warm", 0)


# Sentinel recorded as the deployed "version" for --from-source deploys.
FROM_SOURCE_VERSION = "source"


def _parse_semver(version: str) -> tuple[int, int, int] | None:
    """Parse 'X.Y.Z' into a comparable tuple; None for anything else."""
    parts = version.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    return major, minor, patch


def _record_deployed_server_version(
    app_name: str, version: str, environment_name: str | None = None
) -> None:
    """Persist the just-deployed server version in the app's meta Dict.

    ``FROM_SOURCE_VERSION`` is recorded for from-source deploys.
    """
    import modal

    meta = modal.Dict.from_name(
        _meta_dict_name(app_name),
        create_if_missing=True,
        environment_name=environment_name,
    )
    meta["server_version"] = version


def _deployed_server_version(
    app_name: str, environment_name: str | None = None
) -> str | None:
    """The recorded deployed server version, or None if never recorded."""
    import modal
    import modal.exception

    try:
        meta = modal.Dict.from_name(
            _meta_dict_name(app_name), environment_name=environment_name
        )
    except modal.exception.NotFoundError:
        return None
    return meta.get("server_version")


def _resolve_upgrade_server_version(
    app_name: str, explicit: str | None, environment_name: str | None = None
) -> str:
    """Server version for prebuilt-image `upgrade` runs.

    Resolution order: explicit --server-version flag > recorded deployed
    version > DEFAULT_SERVER_VERSION. Defaulting to the recorded version
    means a plain `upgrade` never silently downgrades a deployment running
    a version newer than this SDK's default (whose migrations may have
    advanced the DB schema past what the default server release knows).
    An explicitly passed older version wins, with a warning.
    """
    deployed = _deployed_server_version(app_name, environment_name)
    if explicit is not None:
        explicit_semver = _parse_semver(explicit)
        deployed_semver = _parse_semver(deployed) if deployed else None
        if (
            explicit_semver is not None
            and deployed_semver is not None
            and explicit_semver < deployed_semver
        ):
            console.print(
                f"[yellow]Warning:[/yellow] deploying server {explicit} over the "
                f"currently deployed {deployed}. Downgrading does not roll back "
                "database migrations, so the older server may run against a "
                "newer schema."
            )
        return explicit
    if deployed == FROM_SOURCE_VERSION:
        console.print(
            "Last deploy was built from source; deploying the prebuilt image "
            f"[bold]{DEFAULT_SERVER_VERSION}[/bold] instead (pass --from-source "
            "to keep building from a local checkout)."
        )
        return DEFAULT_SERVER_VERSION
    if deployed is not None:
        return deployed
    return DEFAULT_SERVER_VERSION


def _ensure_jwt_secret(name: str, environment_name: str | None = None) -> bool:
    """Create the JWT keypair secret if absent. Never overwrites.

    Returns True if a new keypair was created.
    """
    if _secret_exists(name, environment_name):
        return False
    private_pem, public_pem = _generate_jwt_keypair()
    _push_secret(
        name,
        {"JWT_PRIVATE_KEY": private_pem, "JWT_PUBLIC_KEY": public_pem},
        environment_name,
    )
    return True


def _resolve_database_urls(
    database_url: str | None,
    database_url_direct: str | None,
    neon_api_key: str | None,
    neon_project: str,
    interactive: bool,
    neon_pg_version: int = 16,
) -> tuple[str, str, bool]:
    """Resolve (pooled_url, direct_url, pooler_compat) for the deployment.

    Bring-your-own database: pass --database-url (and optionally
    --database-url-direct). Otherwise a Neon project is provisioned
    (created if missing) using the API key.
    """
    if database_url:
        direct = database_url_direct or database_url
        # BYO database: only enable pooler compat when a separate direct
        # URL is supplied (implying the main URL goes through a pooler)
        return database_url, direct, bool(database_url_direct)

    api_key = neon_api_key or os.environ.get("NEON_API_KEY")
    if not api_key:
        if not interactive:
            error_console.print(
                "No database configured: pass --neon-api-key/--database-url "
                "or set NEON_API_KEY."
            )
            raise typer.Exit(1)
        console.print(
            "\n[bold]Postgres database (Neon)[/bold]\n"
            "The Stardag service needs a Postgres database. The easiest "
            "option is a free Neon project (https://neon.com):\n"
            "  1. Sign up at https://console.neon.tech\n"
            "  2. Create an API key: https://console.neon.tech/app/settings/api-keys\n"
        )
        api_key = typer.prompt("Neon API key", hide_input=True)

    console.print(f"Provisioning Neon project [bold]{neon_project}[/bold]...")
    client = NeonClient(api_key)
    try:
        db = client.get_or_create_project(neon_project, pg_version=neon_pg_version)
    except NeonAuthError as e:
        error_console.print(str(e))
        raise typer.Exit(1)
    except NeonError as e:
        error_console.print(f"Neon provisioning failed: {e}")
        raise typer.Exit(1)
    finally:
        client.close()

    if db.created:
        console.print(f"  Created Neon project {db.project_id}")
    else:
        console.print(f"  Using existing Neon project {db.project_id}")

    pooled = to_sqlalchemy_asyncpg_url(db.pooled_uri)
    direct = to_sqlalchemy_asyncpg_url(db.direct_uri)
    return pooled, direct, True


def _provided_config_flags(
    auth_mode: str | None,
    admin_email: str | None,
    admin_password: str | None,
    enable_registration: bool,
    oidc_issuer: str | None,
    oidc_sdk_client_id: str | None,
    oidc_ui_client_id: str | None,
    oidc_audience: str | None,
    oidc_jwks_url: str | None,
) -> list[str]:
    """Names of config-affecting flags the user explicitly provided.

    Used to fail fast when `up` is re-run against an existing config secret
    without database inputs: the secret can only be rewritten as a whole
    (the DB URLs it contains must be re-supplied), so these flags would
    otherwise be silently ignored.
    """
    provided = [
        ("--auth-mode", auth_mode is not None),
        ("--admin-email", admin_email is not None),
        ("--admin-password", admin_password is not None),
        ("--enable-registration", enable_registration),
        ("--oidc-issuer", oidc_issuer is not None),
        ("--oidc-sdk-client-id", oidc_sdk_client_id is not None),
        ("--oidc-ui-client-id", oidc_ui_client_id is not None),
        ("--oidc-audience", oidc_audience is not None),
        ("--oidc-jwks-url", oidc_jwks_url is not None),
    ]
    return [flag for flag, given in provided if given]


def _build_config_env(
    pooled_url: str,
    direct_url: str,
    pooler_compat: bool,
    auth_mode: str,
    admin_email: str | None,
    admin_password: str | None,
    enable_registration: bool,
    oidc_issuer: str | None,
    oidc_sdk_client_id: str | None,
    oidc_ui_client_id: str | None,
    oidc_audience: str | None,
    oidc_jwks_url: str | None,
    primary_workspace_name: str | None = None,
    primary_workspace_environment: str | None = None,
) -> dict[str, str]:
    env = {
        "STARDAG_API_DATABASE_URL": pooled_url,
        "STARDAG_API_DATABASE_URL_DIRECT": direct_url,
        "STARDAG_API_DATABASE_POOLER_COMPAT": "true" if pooler_compat else "false",
        "AUTH_MODE": auth_mode,
        # Self-hosted single-endpoint deployment: no SES
        "EMAIL_ENABLED": "false",
    }
    # Primary workspace/environment bootstrap: acted on by the server in
    # local auth mode (idempotent, anchored on the bootstrap admin). In
    # OIDC mode the server has no known admin at startup, so the CLI's
    # connect flow provisions these via the API instead.
    if primary_workspace_name:
        env["AUTH_PRIMARY_WORKSPACE_NAME"] = primary_workspace_name
    if primary_workspace_environment is not None:
        env["AUTH_PRIMARY_WORKSPACE_ENVIRONMENT"] = primary_workspace_environment
    if auth_mode == "local":
        if admin_email:
            env["AUTH_BOOTSTRAP_ADMIN_EMAIL"] = admin_email
        if admin_password:
            env["AUTH_BOOTSTRAP_ADMIN_PASSWORD"] = admin_password
        env["AUTH_LOCAL_REGISTRATION_ENABLED"] = (
            "true" if enable_registration else "false"
        )
    else:
        assert oidc_issuer is not None
        env["OIDC_ISSUER_URL"] = oidc_issuer
        if oidc_jwks_url:
            env["OIDC_JWKS_URL"] = oidc_jwks_url
        if oidc_audience:
            env["OIDC_AUDIENCE"] = oidc_audience
        if oidc_sdk_client_id:
            env["OIDC_SDK_CLIENT_ID"] = oidc_sdk_client_id
        if oidc_ui_client_id:
            env["OIDC_UI_CLIENT_ID"] = oidc_ui_client_id
    return env


def _deploy(
    repo_root: Path | None,
    app_name: str,
    keep_warm: int,
    server_version: str | None = None,
    run_migrations: bool = True,
    environment_name: str | None = None,
) -> str:
    """Build the Modal app, run migrations, deploy. Returns the web URL.

    Exactly one of ``repo_root`` (build from source) and ``server_version``
    (prebuilt image) should be set - see ``_resolve_image_source``.
    ``environment_name`` is the Modal environment the app (and its secrets)
    live in; None means Modal's default environment.
    """
    import modal

    if repo_root is not None:
        console.print(
            "\nBuilding server app from source "
            "(UI build runs inside the Modal image)..."
        )
        server_app, functions = build_server_app(
            repo_root=repo_root,
            app_name=app_name,
            config_secret_name=f"{app_name}-config",
            jwt_secret_name=f"{app_name}-jwt",
            keep_warm=keep_warm,
            environment_name=environment_name,
        )
    else:
        version = server_version or DEFAULT_SERVER_VERSION
        console.print(
            f"\nUsing prebuilt server image [bold]{server_image_ref(version)}[/bold]..."
        )
        server_app, functions = build_server_app(
            app_name=app_name,
            config_secret_name=f"{app_name}-config",
            jwt_secret_name=f"{app_name}-jwt",
            keep_warm=keep_warm,
            server_version=version,
            environment_name=environment_name,
        )

    try:
        with modal.enable_output():
            if run_migrations:
                console.print("Applying database migrations...")
                with server_app.run(environment_name=environment_name):
                    output = functions["migrate"].remote()
                for line in output.strip().splitlines()[-5:]:
                    console.print(f"  [dim]{line}[/dim]")

            console.print("Deploying...")
            server_app.deploy(environment_name=environment_name)
    except Exception:
        if repo_root is None:
            error_console.print(
                f"\nDeployment with the prebuilt image "
                f"{server_image_ref(server_version or DEFAULT_SERVER_VERSION)} "
                "failed (see output above). If the image could not be pulled "
                "(e.g. the version does not exist yet, or the GHCR package "
                "is not public yet), retry with --from-source from a "
                "checkout of https://github.com/stardag-dev/stardag."
            )
        raise

    url = functions["web"].get_web_url()
    if not url:
        # Fall back to looking up the deployed function
        url = modal.Function.from_name(
            app_name, "web", environment_name=environment_name
        ).get_web_url()
    return url or "<unknown - check `modal app list`>"


def _deployed_web_url(app_name: str, environment_name: str | None) -> str | None:
    """Web URL of an already-deployed server app, or None if not deployed."""
    import modal
    import modal.exception

    try:
        return modal.Function.from_name(
            app_name, "web", environment_name=environment_name
        ).get_web_url()
    except modal.exception.NotFoundError:
        return None


# --- commands ----------------------------------------------------------------


@app.command()
def up(
    server_version: str | None = typer.Option(
        None,
        "--server-version",
        help="Prebuilt server image version to deploy: X.Y.Z or 'latest' "
        f"(default: {DEFAULT_SERVER_VERSION}, the version this SDK release "
        "is tested against)",
    ),
    from_source: bool = typer.Option(
        False,
        "--from-source",
        help="Build the image from a local stardag repo checkout instead of "
        "using the prebuilt image (development workflow)",
    ),
    repo: Path = typer.Option(
        None,
        "--repo",
        help="Path to the stardag repo checkout (implies --from-source; "
        "default: auto-detect from cwd)",
    ),
    name: str = typer.Option(
        DEFAULT_APP_NAME, "--name", help="Modal app name (and URL label)"
    ),
    neon_api_key: str = typer.Option(
        None,
        "--neon-api-key",
        help="Neon API key for database provisioning (or set NEON_API_KEY)",
        envvar="NEON_API_KEY",
    ),
    neon_project: str = typer.Option(
        DEFAULT_NEON_PROJECT,
        "--neon-project",
        help="Neon project name to find-or-create",
    ),
    neon_pg_version: int = typer.Option(
        16,
        "--neon-pg-version",
        help="Postgres major version when creating the Neon project "
        "(default 16 = what stardag is tested against; existing projects "
        "keep their version)",
    ),
    database_url: str = typer.Option(
        None,
        "--database-url",
        help="Bring-your-own Postgres URL (skips Neon provisioning). "
        "Use the SQLAlchemy asyncpg form: postgresql+asyncpg://...",
    ),
    database_url_direct: str = typer.Option(
        None,
        "--database-url-direct",
        help="Direct (non-pooled) variant of --database-url, used for migrations",
    ),
    auth_mode: str = typer.Option(
        None,
        "--auth-mode",
        help="Authentication mode: 'local' (email/password, default) or 'oidc'",
    ),
    admin_email: str = typer.Option(
        None, "--admin-email", help="Bootstrap admin email (local auth mode)"
    ),
    admin_password: str = typer.Option(
        None,
        "--admin-password",
        help="Bootstrap admin password (local auth mode). Prompted if omitted.",
    ),
    enable_registration: bool = typer.Option(
        False,
        "--enable-registration",
        help="Allow self-service signup (local auth mode). Off by default.",
    ),
    oidc_issuer: str = typer.Option(
        None, "--oidc-issuer", help="OIDC issuer URL (oidc auth mode)"
    ),
    oidc_sdk_client_id: str = typer.Option(
        None, "--oidc-sdk-client-id", help="OIDC client ID for the SDK/CLI"
    ),
    oidc_ui_client_id: str = typer.Option(
        None, "--oidc-ui-client-id", help="OIDC client ID for the web UI"
    ),
    oidc_audience: str = typer.Option(
        None,
        "--oidc-audience",
        help="Accepted token audiences, comma-separated (oidc auth mode)",
    ),
    oidc_jwks_url: str = typer.Option(
        None,
        "--oidc-jwks-url",
        help="JWKS URL (only needed if not at <issuer>/protocol/openid-connect/certs)",
    ),
    keep_warm: int = typer.Option(
        None,
        "--keep-warm",
        help="Containers to keep always-on (0 = scale to zero, a few seconds "
        "cold start on first request). Persisted: when omitted, the last "
        "explicitly set value is kept (initially 0).",
    ),
    server_modal_env: str = typer.Option(
        DEFAULT_SERVER_MODAL_ENV,
        "--server-modal-env",
        help="Modal environment for the server app + its secrets (created if "
        "missing). Keeps the server isolated from the environments where "
        "your DAG apps run. Pass '' for Modal's default environment.",
    ),
    primary_workspace: str = typer.Option(
        None,
        "--primary-workspace",
        help="Name of the primary (shared) Stardag workspace to create. "
        "Default: your shared Modal workspace's name; skipped for personal "
        "Modal workspaces (your personal Stardag workspace is used instead).",
    ),
    no_primary_workspace: bool = typer.Option(
        False,
        "--no-primary-workspace",
        help="Do not create/map a shared primary workspace.",
    ),
    skip_connect: bool = typer.Option(
        False,
        "--skip-connect",
        help="Skip the post-deploy setup (workspace wiring, API key, local "
        "SDK profile). Run it later with `stardag self-host connect`.",
    ),
    execution_modal_env: str = typer.Option(
        None,
        "--execution-modal-env",
        help="Modal environment where your DAG apps run - the stardag-api-key "
        "secret is pushed there (default: your Modal account's default "
        "environment, typically 'main'). NOT the server's environment.",
    ),
    target_root: str = typer.Option(
        None,
        "--target-root",
        help="Default target root for the primary environment as name=uri "
        "(default: default=modalvol://stardag/<workspace-slug>).",
    ),
    no_target_root: bool = typer.Option(
        False, "--no-target-root", help="Skip creating a default target root."
    ),
    registry_name: str = typer.Option(
        "selfhosted",
        "--registry-name",
        help="Name for the registry entry written to the local SDK config.",
    ),
    profile_name: str = typer.Option(
        "selfhosted",
        "--profile-name",
        help="Name for the profile written to the local SDK config.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Non-interactive: take defaults, fail on prompts"
    ),
):
    """Bring up the full Stardag stack: database, migrations, API + UI on Modal.

    After the deploy, completes the setup (unless --skip-connect): primary
    workspace + 'main' environment, an API key pushed as the Modal secret
    'stardag-api-key' into your DAG-execution Modal environment, a default
    target root, and a local SDK registry + profile.
    """
    interactive = not yes
    repo_root, resolved_server_version = _resolve_image_source(
        repo, from_source, server_version
    )
    if repo_root is not None:
        console.print(f"Using stardag repo: [bold]{repo_root}[/bold]")
    target_root_override = parse_target_root_flag(target_root) if target_root else None

    modal_info = _check_modal_auth()
    if modal_info.workspace_name:
        console.print(
            f"Modal workspace: [bold]{modal_info.workspace_name}[/bold] "
            f"(shared, signed in as {modal_info.username})"
        )
    else:
        console.print(f"Modal workspace: [bold]{modal_info.username}[/bold] (personal)")

    server_env = server_modal_env or None
    if server_env:
        if _ensure_modal_environment(server_env):
            console.print(
                f"Created Modal environment [bold]{server_env}[/bold] for the "
                "server (isolated from the environments where your DAG apps run)."
            )
        else:
            console.print(f"Server Modal environment: [bold]{server_env}[/bold]")

    config_secret_name = f"{name}-config"
    jwt_secret_name = f"{name}-jwt"
    config_exists = _secret_exists(config_secret_name, server_env)

    # --- Database ---
    if config_exists and not (database_url or neon_api_key):
        # Re-run without new DB config: keep the existing configuration.
        # The config secret is only ever rewritten as a whole (its DB URLs
        # must be re-supplied), so fail fast if the user passed auth/config
        # flags that would otherwise be silently ignored.
        overrides = _provided_config_flags(
            auth_mode,
            admin_email,
            admin_password,
            enable_registration,
            oidc_issuer,
            oidc_sdk_client_id,
            oidc_ui_client_id,
            oidc_audience,
            oidc_jwks_url,
        )
        if overrides:
            error_console.print(
                f"Config secret {config_secret_name!r} already exists; "
                f"{', '.join(overrides)} cannot be applied without rewriting "
                "it, which requires re-supplying the database configuration. "
                "Re-run with --neon-api-key or --database-url to reconfigure."
            )
            raise typer.Exit(1)
        console.print(
            f"Config secret [bold]{config_secret_name}[/bold] exists - "
            "keeping current configuration (pass --neon-api-key or "
            "--database-url to reconfigure)."
        )
        pooled_url = direct_url = None
        pooler_compat = False
    else:
        pooled_url, direct_url, pooler_compat = _resolve_database_urls(
            database_url,
            database_url_direct,
            neon_api_key,
            neon_project,
            interactive,
            neon_pg_version=neon_pg_version,
        )

    # --- Auth config (only when [re]writing the config secret) ---
    primary_ws_name: str | None = None
    primary_ws_resolved = False
    if pooled_url is not None:
        if auth_mode is None:
            if interactive:
                auth_mode = typer.prompt(
                    "Auth mode: 'local' (email/password) or 'oidc' (external provider)",
                    default="local",
                )
            else:
                auth_mode = "local"
        if auth_mode not in ("local", "oidc"):
            error_console.print(f"Invalid --auth-mode: {auth_mode}")
            raise typer.Exit(1)

        if auth_mode == "local":
            if not admin_email:
                if not interactive:
                    error_console.print("--admin-email is required with --yes")
                    raise typer.Exit(1)
                admin_email = typer.prompt("Admin email (first login account)")
            if not admin_password:
                if not interactive:
                    error_console.print("--admin-password is required with --yes")
                    raise typer.Exit(1)
                admin_password = typer.prompt(
                    "Admin password (min 8 chars)",
                    hide_input=True,
                    confirmation_prompt=True,
                )
            password_error = _admin_password_error(admin_password)
            if password_error:
                error_console.print(password_error)
                raise typer.Exit(1)
        else:
            if not oidc_issuer:
                if not interactive:
                    error_console.print("--oidc-issuer is required with --yes")
                    raise typer.Exit(1)
                oidc_issuer = typer.prompt("OIDC issuer URL")
            if not oidc_ui_client_id:
                if not interactive:
                    error_console.print("--oidc-ui-client-id is required with --yes")
                    raise typer.Exit(1)
                oidc_ui_client_id = typer.prompt("OIDC client ID (web UI)")
            if not oidc_sdk_client_id:
                oidc_sdk_client_id = (
                    typer.prompt("OIDC client ID (SDK/CLI)", default=oidc_ui_client_id)
                    if interactive
                    else oidc_ui_client_id
                )
            if not oidc_audience and interactive:
                oidc_audience = typer.prompt(
                    "Accepted token audiences (comma-separated)",
                    default=f"{oidc_ui_client_id},{oidc_sdk_client_id}",
                )

        # --- Primary workspace mapping (baked into the config secret so the
        # server bootstraps it on first boot; local mode only - in OIDC mode
        # only an explicit --primary-workspace is passed through and the
        # connect flow creates it via the API after login) ---
        if auth_mode == "local":
            primary_ws_name = resolve_primary_workspace(
                primary_workspace,
                no_primary_workspace,
                modal_info.workspace_name,
                interactive,
            )
        else:
            primary_ws_name = primary_workspace if not no_primary_workspace else None
        primary_ws_resolved = True

        env = _build_config_env(
            pooled_url,
            direct_url,  # type: ignore[arg-type]
            pooler_compat,
            auth_mode,
            admin_email,
            admin_password,
            enable_registration,
            oidc_issuer,
            oidc_sdk_client_id,
            oidc_ui_client_id,
            oidc_audience,
            oidc_jwks_url,
            primary_workspace_name=primary_ws_name,
            primary_workspace_environment=PRIMARY_ENVIRONMENT_SLUG,
        )
        console.print(f"Writing config secret [bold]{config_secret_name}[/bold]...")
        _push_secret(config_secret_name, env, server_env)

    # --- JWT keypair (create once, never overwrite) ---
    if _ensure_jwt_secret(jwt_secret_name, server_env):
        console.print(
            f"Generated JWT signing keypair -> [bold]{jwt_secret_name}[/bold]"
        )
    else:
        console.print(
            f"JWT keypair secret [bold]{jwt_secret_name}[/bold] exists - kept."
        )

    # --- Migrate + deploy ---
    url = _deploy(
        repo_root,
        name,
        _resolve_keep_warm(name, keep_warm, server_env),
        server_version=resolved_server_version,
        environment_name=server_env,
    )
    _record_deployed_server_version(
        name, resolved_server_version or FROM_SOURCE_VERSION, server_env
    )

    console.print("\n[bold green]Stardag is up![/bold green]")
    console.print(f"\n  UI:  [bold]{url}[/bold]")
    console.print(f"  API: {url}/api/v1")
    if auth_mode == "oidc" and oidc_issuer:
        console.print(
            f"\n(Make sure {url}/callback is an allowed redirect URI "
            "in your OIDC provider.)"
        )

    # --- Post-deploy connect phase ---
    if auth_mode is None:
        # Re-run against an existing config secret: ask the deployed service
        auth_config = get_auth_config(url)
        auth_mode = (auth_config or {}).get("auth_mode")

    if skip_connect:
        _print_connect_pointer(auth_mode)
        return

    if auth_mode != "local":
        # OIDC (or unknown): the connect flow needs a browser login first
        _print_connect_pointer(auth_mode)
        return

    if not (admin_email and admin_password):
        # Re-run without fresh admin credentials: prompt, or point to connect
        if not interactive:
            _print_connect_pointer(auth_mode)
            return
        console.print("\nSign in to complete the setup (admin account):")
        admin_email = typer.prompt("Email")
        admin_password = typer.prompt("Password", hide_input=True)

    if interactive and not typer.confirm(
        "Complete the setup now (workspace, API key for Modal DAG execution, "
        "local SDK profile)?",
        default=True,
    ):
        _print_connect_pointer(auth_mode)
        return

    if not primary_ws_resolved:
        primary_ws_name = resolve_primary_workspace(
            primary_workspace,
            no_primary_workspace,
            modal_info.workspace_name,
            interactive,
        )

    console.print()
    session_token = login_local(url, admin_email, admin_password, registry_name)
    outcome = run_connect(
        url,
        session_token,
        admin_email,
        primary_workspace=primary_ws_name,
        execution_modal_env=execution_modal_env,
        target_root=target_root_override,
        no_target_root=no_target_root,
        registry_name=registry_name,
        profile_name=profile_name,
    )
    console.print()
    print_summary(outcome, name, server_env)


def _print_connect_pointer(auth_mode: str | None) -> None:
    """Next-steps output when the connect phase is skipped or deferred."""
    console.print("\nNext steps:")
    if auth_mode == "local":
        console.print(
            "  1. Complete the setup (workspace, API key for Modal DAG "
            "execution, local SDK profile):\n"
            "     [bold]stardag self-host connect[/bold]"
        )
    else:
        console.print(
            "  1. Complete the setup (sign in via your identity provider, "
            "then workspace, API key, local SDK profile):\n"
            "     [bold]stardag self-host connect[/bold]"
        )
    console.print("  2. To update to a newer server release: stardag self-host upgrade")


@app.command()
def upgrade(
    server_version: str | None = typer.Option(
        None,
        "--server-version",
        help="Prebuilt server image version to deploy: X.Y.Z or 'latest' "
        "(default: the currently deployed version, falling back to "
        f"{DEFAULT_SERVER_VERSION}, the version this SDK release is tested "
        "against)",
    ),
    from_source: bool = typer.Option(
        False,
        "--from-source",
        help="Build the image from a local stardag repo checkout instead of "
        "using the prebuilt image (development workflow)",
    ),
    repo: Path = typer.Option(
        None,
        "--repo",
        help="Path to the stardag repo checkout (implies --from-source; "
        "default: auto-detect from cwd)",
    ),
    name: str = typer.Option(DEFAULT_APP_NAME, "--name", help="Modal app name"),
    keep_warm: int = typer.Option(
        None,
        "--keep-warm",
        help="Containers to keep always-on. Persisted: when omitted, the "
        "last explicitly set value is kept (initially 0).",
    ),
    server_modal_env: str = typer.Option(
        DEFAULT_SERVER_MODAL_ENV,
        "--server-modal-env",
        help="Modal environment the server app + secrets live in. Pass '' "
        "for Modal's default environment (deployments made before the "
        "dedicated server environment existed).",
    ),
):
    """Update the deployment: apply DB migrations and redeploy."""
    repo_root, resolved_server_version = _resolve_image_source(
        repo, from_source, server_version
    )
    if repo_root is not None:
        console.print(f"Using stardag repo: [bold]{repo_root}[/bold]")
    modal_info = _check_modal_auth()
    console.print(f"Modal workspace: [bold]{modal_info.display}[/bold]")
    server_env = server_modal_env or None
    if server_env:
        console.print(f"Server Modal environment: [bold]{server_env}[/bold]")

    for secret_name in (f"{name}-config", f"{name}-jwt"):
        if not _secret_exists(secret_name, server_env):
            error_console.print(
                f"Secret {secret_name!r} not found"
                + (f" in Modal environment {server_env!r}" if server_env else "")
                + " - run `stardag self-host up` first. (Deployed with an "
                "older SDK? Its objects live in your default Modal "
                'environment - pass --server-modal-env "".)'
            )
            raise typer.Exit(1)

    if repo_root is None:
        # Prebuilt path: default to the recorded deployed version so a plain
        # `upgrade` never silently downgrades (explicit flag still wins).
        resolved_server_version = _resolve_upgrade_server_version(
            name, server_version, server_env
        )

    url = _deploy(
        repo_root,
        name,
        _resolve_keep_warm(name, keep_warm, server_env),
        server_version=resolved_server_version,
        environment_name=server_env,
    )
    _record_deployed_server_version(
        name, resolved_server_version or FROM_SOURCE_VERSION, server_env
    )
    console.print("\n[bold green]Upgrade complete.[/bold green]")
    console.print(f"  UI: [bold]{url}[/bold]")


@app.command()
def connect(
    name: str = typer.Option(
        DEFAULT_APP_NAME, "--name", help="Modal app name of the deployed server"
    ),
    url: str = typer.Option(
        None,
        "--url",
        help="Server URL (default: looked up from the deployed Modal app)",
    ),
    server_modal_env: str = typer.Option(
        DEFAULT_SERVER_MODAL_ENV,
        "--server-modal-env",
        help="Modal environment the server app lives in (for the URL lookup). "
        "Pass '' for Modal's default environment.",
    ),
    admin_email: str = typer.Option(
        None, "--admin-email", help="Email to sign in with (local auth mode)"
    ),
    admin_password: str = typer.Option(
        None,
        "--admin-password",
        help="Password to sign in with (local auth mode). Prompted if omitted.",
    ),
    primary_workspace: str = typer.Option(
        None,
        "--primary-workspace",
        help="Name of the primary (shared) Stardag workspace. Default: your "
        "shared Modal workspace's name; skipped for personal Modal "
        "workspaces (your personal Stardag workspace is used instead).",
    ),
    no_primary_workspace: bool = typer.Option(
        False,
        "--no-primary-workspace",
        help="Do not create/map a shared primary workspace.",
    ),
    execution_modal_env: str = typer.Option(
        None,
        "--execution-modal-env",
        help="Modal environment where your DAG apps run - the stardag-api-key "
        "secret is pushed there (default: your Modal account's default "
        "environment, typically 'main').",
    ),
    target_root: str = typer.Option(
        None,
        "--target-root",
        help="Default target root for the primary environment as name=uri "
        "(default: default=modalvol://stardag/<workspace-slug>).",
    ),
    no_target_root: bool = typer.Option(
        False, "--no-target-root", help="Skip creating a default target root."
    ),
    registry_name: str = typer.Option(
        "selfhosted",
        "--registry-name",
        help="Name for the registry entry written to the local SDK config.",
    ),
    profile_name: str = typer.Option(
        "selfhosted",
        "--profile-name",
        help="Name for the profile written to the local SDK config.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Non-interactive: take defaults, fail on prompts"
    ),
):
    """Complete (or re-run) the post-deploy setup for a deployed server.

    Idempotent: ensures the primary workspace + 'main' environment, an API
    key pushed as the Modal secret 'stardag-api-key' into your DAG-execution
    Modal environment, a default target root, and a local SDK registry +
    profile. In OIDC auth mode this signs you in via the browser first.
    """
    interactive = not yes
    target_root_override = parse_target_root_flag(target_root) if target_root else None

    modal_info = _check_modal_auth()
    server_env = server_modal_env or None

    resolved_url = url or _deployed_web_url(name, server_env)
    if not resolved_url:
        error_console.print(
            f"App {name!r} is not deployed"
            + (f" in Modal environment {server_env!r}" if server_env else "")
            + " - run `stardag self-host up` first (or pass --url)."
        )
        raise typer.Exit(1)
    resolved_url = resolved_url.rstrip("/")
    console.print(f"Server: [bold]{resolved_url}[/bold]")

    auth_config = get_auth_config(resolved_url)
    if auth_config is None:
        error_console.print(
            f"Could not fetch auth configuration from {resolved_url} - is the "
            "service healthy? (Check `stardag self-host status` and the logs.)"
        )
        raise typer.Exit(1)

    if auth_config.get("auth_mode") == "local":
        if not admin_email:
            if not interactive:
                error_console.print("--admin-email is required with --yes")
                raise typer.Exit(1)
            admin_email = typer.prompt("Email")
        if not admin_password:
            if not interactive:
                error_console.print("--admin-password is required with --yes")
                raise typer.Exit(1)
            admin_password = typer.prompt("Password", hide_input=True)
        bearer_token = login_local(
            resolved_url, admin_email, admin_password, registry_name
        )
        user_email = admin_email
    else:
        if not interactive:
            error_console.print(
                "This registry uses OIDC authentication, which needs a "
                "browser login. Re-run without --yes, or authenticate first "
                f"with `stardag auth login -r {registry_name} --api-url "
                f"{resolved_url}` and re-run."
            )
            raise typer.Exit(1)
        # Same browser PKCE flow as `stardag auth login`
        from stardag._cli.auth import _login_oidc_flow
        from stardag._cli.credentials import add_registry

        add_registry(registry_name, resolved_url)
        bearer_token, user_email = _login_oidc_flow(
            registry=registry_name,
            api_url=resolved_url,
            oidc_issuer=None,
            client_id=None,
            auth_config=auth_config,
        )

    primary_ws_name = resolve_primary_workspace(
        primary_workspace,
        no_primary_workspace,
        modal_info.workspace_name,
        interactive,
    )

    console.print()
    outcome = run_connect(
        resolved_url,
        bearer_token,
        user_email,
        primary_workspace=primary_ws_name,
        execution_modal_env=execution_modal_env,
        target_root=target_root_override,
        no_target_root=no_target_root,
        registry_name=registry_name,
        profile_name=profile_name,
    )
    console.print()
    print_summary(outcome, name, server_env)


@app.command()
def status(
    name: str = typer.Option(DEFAULT_APP_NAME, "--name", help="Modal app name"),
    server_modal_env: str = typer.Option(
        DEFAULT_SERVER_MODAL_ENV,
        "--server-modal-env",
        help="Modal environment the server app + secrets live in. Pass '' "
        "for Modal's default environment.",
    ),
):
    """Show deployment status."""
    import modal
    import modal.exception

    _check_modal_auth()
    server_env = server_modal_env or None
    if server_env:
        console.print(f"Server Modal environment: [bold]{server_env}[/bold]")

    try:
        web = modal.Function.from_name(name, "web", environment_name=server_env)
        url = web.get_web_url()
        console.print(f"App [bold]{name}[/bold]: deployed")
        console.print(f"  UI: [bold]{url}[/bold]")
    except modal.exception.NotFoundError:
        console.print(f"App [bold]{name}[/bold]: not deployed")

    for secret_name in (f"{name}-config", f"{name}-jwt"):
        state = "present" if _secret_exists(secret_name, server_env) else "missing"
        console.print(f"  Secret {secret_name}: {state}")

    try:
        meta = modal.Dict.from_name(_meta_dict_name(name), environment_name=server_env)
        server_version = meta.get("server_version")
        if server_version == FROM_SOURCE_VERSION:
            console.print("  Server version: built from source")
        elif server_version:
            console.print(f"  Server version: {server_version}")
        console.print(f"  Keep-warm containers: {meta.get('keep_warm', 0)}")
    except modal.exception.NotFoundError:
        pass


@app.command()
def destroy(
    name: str = typer.Option(DEFAULT_APP_NAME, "--name", help="Modal app name"),
    delete_secrets: bool = typer.Option(
        False,
        "--delete-secrets",
        help="Also delete the config + JWT secrets and the settings Dict "
        "(existing sessions and SDK logins become invalid)",
    ),
    server_modal_env: str = typer.Option(
        DEFAULT_SERVER_MODAL_ENV,
        "--server-modal-env",
        help="Modal environment the server app + secrets live in. Pass '' "
        "for Modal's default environment.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Stop the Modal app. The database (Neon project) is never touched."""
    import subprocess
    import sys

    if not yes:
        typer.confirm(
            f"Stop the Modal app {name!r}? (The database is left untouched.)",
            abort=True,
        )

    _check_modal_auth()
    server_env = server_modal_env or None
    stop_command = [sys.executable, "-m", "modal", "app", "stop", name]
    if server_env:
        stop_command += ["--env", server_env]
    result = subprocess.run(
        stop_command,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error_console.print(f"Failed to stop app: {result.stderr.strip()}")
        raise typer.Exit(1)
    console.print(f"App [bold]{name}[/bold] stopped.")

    if delete_secrets:
        import modal

        for secret_name in (f"{name}-config", f"{name}-jwt"):
            modal.Secret.objects.delete(
                secret_name, allow_missing=True, environment_name=server_env
            )
            console.print(f"Deleted secret {secret_name}")
        modal.Dict.objects.delete(
            _meta_dict_name(name), allow_missing=True, environment_name=server_env
        )
        console.print(f"Deleted dict {_meta_dict_name(name)}")

    console.print(
        "\nNote: the Neon project/database was NOT deleted. Manage it at "
        "https://console.neon.tech if you want to remove it. The Modal "
        "environment is also kept (delete with `modal environment delete`)."
    )
