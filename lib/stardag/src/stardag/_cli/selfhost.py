"""CLI for self-hosting the Stardag service (API + UI) on Modal.

Usage:
    stardag self-host up          # provision + deploy the full stack
    stardag self-host upgrade     # migrate DB + redeploy from current source
    stardag self-host status      # show deployment status and URL
    stardag self-host destroy     # stop the Modal app (DB is left untouched)

Requires the `selfhost` extra: pip install "stardag[selfhost]"
Run from a checkout of the stardag repo (or pass --repo).
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console

from stardag.selfhost._modal_app import (
    DEFAULT_APP_NAME,
    build_server_app,
    find_repo_root,
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
            "app/stardag-api and app/stardag-ui). Run this command from a "
            "clone of https://github.com/stardag-dev/stardag or pass --repo."
        )
        raise typer.Exit(1)
    return root


def _check_modal_auth() -> str:
    """Verify Modal credentials; returns the workspace name."""
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
        return workspace.username
    except Exception as e:
        error_console.print(f"Modal authentication not set up: {e}")
        console.print(
            "\nRun [bold]modal token new[/bold] (or [bold]uv run modal token new[/bold]) "
            "to authenticate with Modal first. Create a free account at "
            "https://modal.com if you don't have one."
        )
        raise typer.Exit(1)


def _secret_exists(name: str) -> bool:
    import modal
    import modal.exception

    try:
        modal.Secret.from_name(name).hydrate()
        return True
    except modal.exception.NotFoundError:
        return False


def _push_secret(name: str, env: dict[str, str]) -> None:
    """Create or replace a named Modal secret."""
    import modal

    modal.Secret.objects.delete(name, allow_missing=True)
    modal.Secret.objects.create(name, env)


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


def _resolve_keep_warm(app_name: str, keep_warm: int | None) -> int:
    """Resolve the effective keep-warm value, persisting it across deploys.

    An explicitly provided value wins and is stored in the app's meta Dict;
    when the flag is omitted the previously stored value is used (default 0),
    so a plain `upgrade` doesn't silently reset keep-warm to scale-to-zero.
    """
    import modal

    meta = modal.Dict.from_name(_meta_dict_name(app_name), create_if_missing=True)
    if keep_warm is not None:
        meta["keep_warm"] = keep_warm
        return keep_warm
    return meta.get("keep_warm", 0)


def _ensure_jwt_secret(name: str) -> bool:
    """Create the JWT keypair secret if absent. Never overwrites.

    Returns True if a new keypair was created.
    """
    if _secret_exists(name):
        return False
    private_pem, public_pem = _generate_jwt_keypair()
    _push_secret(name, {"JWT_PRIVATE_KEY": private_pem, "JWT_PUBLIC_KEY": public_pem})
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
) -> dict[str, str]:
    env = {
        "STARDAG_API_DATABASE_URL": pooled_url,
        "STARDAG_API_DATABASE_URL_DIRECT": direct_url,
        "STARDAG_API_DATABASE_POOLER_COMPAT": "true" if pooler_compat else "false",
        "AUTH_MODE": auth_mode,
        # Self-hosted single-endpoint deployment: no SES
        "EMAIL_ENABLED": "false",
    }
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
    repo_root: Path,
    app_name: str,
    keep_warm: int,
    run_migrations: bool = True,
) -> str:
    """Build the Modal app, run migrations, deploy. Returns the web URL."""
    import modal

    console.print("\nBuilding server app (UI build runs inside the Modal image)...")
    server_app, functions = build_server_app(
        repo_root=repo_root,
        app_name=app_name,
        config_secret_name=f"{app_name}-config",
        jwt_secret_name=f"{app_name}-jwt",
        keep_warm=keep_warm,
    )

    with modal.enable_output():
        if run_migrations:
            console.print("Applying database migrations...")
            with server_app.run():
                output = functions["migrate"].remote()
            for line in output.strip().splitlines()[-5:]:
                console.print(f"  [dim]{line}[/dim]")

        console.print("Deploying...")
        server_app.deploy()

    url = functions["web"].get_web_url()
    if not url:
        # Fall back to looking up the deployed function
        url = modal.Function.from_name(app_name, "web").get_web_url()
    return url or "<unknown - check `modal app list`>"


# --- commands ----------------------------------------------------------------


@app.command()
def up(
    repo: Path = typer.Option(
        None, "--repo", help="Path to the stardag repo checkout (default: auto-detect)"
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
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Non-interactive: fail instead of prompting"
    ),
):
    """Bring up the full Stardag stack: database, migrations, API + UI on Modal."""
    interactive = not yes
    repo_root = _require_repo_root(repo)
    console.print(f"Using stardag repo: [bold]{repo_root}[/bold]")

    workspace = _check_modal_auth()
    console.print(f"Modal workspace: [bold]{workspace}[/bold]")

    config_secret_name = f"{name}-config"
    jwt_secret_name = f"{name}-jwt"
    config_exists = _secret_exists(config_secret_name)

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
        )
        console.print(f"Writing config secret [bold]{config_secret_name}[/bold]...")
        _push_secret(config_secret_name, env)

    # --- JWT keypair (create once, never overwrite) ---
    if _ensure_jwt_secret(jwt_secret_name):
        console.print(
            f"Generated JWT signing keypair -> [bold]{jwt_secret_name}[/bold]"
        )
    else:
        console.print(
            f"JWT keypair secret [bold]{jwt_secret_name}[/bold] exists - kept."
        )

    # --- Migrate + deploy ---
    url = _deploy(repo_root, name, _resolve_keep_warm(name, keep_warm))

    console.print("\n[bold green]Stardag is up![/bold green]")
    console.print(f"\n  UI:  [bold]{url}[/bold]")
    console.print(f"  API: {url}/api/v1")
    console.print("\nNext steps:")
    console.print(
        "  1. Open the UI and sign in"
        + (
            " with the admin account you just configured."
            if auth_mode == "local"
            else "."
        )
    )
    if auth_mode == "oidc" and oidc_issuer:
        console.print(
            f"     (Make sure {url}/callback is an allowed redirect URI "
            "in your OIDC provider.)"
        )
    console.print(
        "  2. Point the SDK at your registry:\n"
        f"     stardag config registry add selfhosted --url {url}\n"
        "     stardag auth login -r selfhosted"
    )
    console.print("  3. To update after pulling new code: stardag self-host upgrade")


@app.command()
def upgrade(
    repo: Path = typer.Option(None, "--repo", help="Path to the stardag repo checkout"),
    name: str = typer.Option(DEFAULT_APP_NAME, "--name", help="Modal app name"),
    keep_warm: int = typer.Option(
        None,
        "--keep-warm",
        help="Containers to keep always-on. Persisted: when omitted, the "
        "last explicitly set value is kept (initially 0).",
    ),
):
    """Update the deployment: apply DB migrations and redeploy from current source."""
    repo_root = _require_repo_root(repo)
    console.print(f"Using stardag repo: [bold]{repo_root}[/bold]")
    workspace = _check_modal_auth()
    console.print(f"Modal workspace: [bold]{workspace}[/bold]")

    for secret_name in (f"{name}-config", f"{name}-jwt"):
        if not _secret_exists(secret_name):
            error_console.print(
                f"Secret {secret_name!r} not found - run `stardag self-host up` first."
            )
            raise typer.Exit(1)

    url = _deploy(repo_root, name, _resolve_keep_warm(name, keep_warm))
    console.print("\n[bold green]Upgrade complete.[/bold green]")
    console.print(f"  UI: [bold]{url}[/bold]")


@app.command()
def status(
    name: str = typer.Option(DEFAULT_APP_NAME, "--name", help="Modal app name"),
):
    """Show deployment status."""
    import modal
    import modal.exception

    _check_modal_auth()

    try:
        web = modal.Function.from_name(name, "web")
        url = web.get_web_url()
        console.print(f"App [bold]{name}[/bold]: deployed")
        console.print(f"  UI: [bold]{url}[/bold]")
    except modal.exception.NotFoundError:
        console.print(f"App [bold]{name}[/bold]: not deployed")

    for secret_name in (f"{name}-config", f"{name}-jwt"):
        state = "present" if _secret_exists(secret_name) else "missing"
        console.print(f"  Secret {secret_name}: {state}")

    try:
        meta = modal.Dict.from_name(_meta_dict_name(name))
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
    result = subprocess.run(
        [sys.executable, "-m", "modal", "app", "stop", name],
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
            modal.Secret.objects.delete(secret_name, allow_missing=True)
            console.print(f"Deleted secret {secret_name}")
        modal.Dict.objects.delete(_meta_dict_name(name), allow_missing=True)
        console.print(f"Deleted dict {_meta_dict_name(name)}")

    console.print(
        "\nNote: the Neon project/database was NOT deleted. Manage it at "
        "https://console.neon.tech if you want to remove it."
    )
