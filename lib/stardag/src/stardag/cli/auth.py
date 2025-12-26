"""Authentication commands for Stardag CLI.

Supports two authentication modes:
1. API Key (production): Set STARDAG_API_KEY environment variable
2. Browser login: OAuth/OIDC flow with any compliant provider + token exchange

Token Model:
- OIDC tokens are user-scoped (used only for /auth/exchange)
- Internal tokens are org-scoped (used for all other API calls)
- Refresh tokens stored per registry in ~/.stardag/credentials/{registry}.json
- Access tokens cached per (registry, org) in ~/.stardag/access-token-cache/

Supported OIDC Providers:
- Local dev: Keycloak
- Production: AWS Cognito (with Google federation)
"""

import base64
import hashlib
import http.server
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass

import typer

from stardag.cli.credentials import (
    add_profile,
    add_registry,
    clear_credentials,
    get_config_path,
    get_credentials_path,
    get_registry_url,
    list_profiles,
    load_credentials,
    save_access_token_cache,
    save_credentials,
    set_default_profile,
    set_target_roots,
)
from stardag.config import cache_org_id, cache_workspace_id, get_config

app = typer.Typer(help="Authentication commands for Stardag API")

# Default OIDC configuration for local Keycloak
DEFAULT_OIDC_ISSUER = "http://localhost:8080/realms/stardag"
DEFAULT_OIDC_CLIENT_ID = "stardag-sdk"
DEFAULT_API_URL = "http://localhost:8000"
CALLBACK_PORT = 8400


@dataclass
class AuthResult:
    """Result from OAuth callback."""

    code: str | None = None
    error: str | None = None


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback."""

    auth_result: AuthResult | None = None

    def do_GET(self):
        """Handle GET request from OAuth redirect."""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            OAuthCallbackHandler.auth_result = AuthResult(code=params["code"][0])
            self._send_success_response()
        elif "error" in params:
            error = params.get("error", ["unknown"])[0]
            error_desc = params.get("error_description", [""])[0]
            OAuthCallbackHandler.auth_result = AuthResult(
                error=f"{error}: {error_desc}"
            )
            self._send_error_response(error, error_desc)
        else:
            OAuthCallbackHandler.auth_result = AuthResult(
                error="No code or error in callback"
            )
            self._send_error_response(
                "invalid_response", "No authorization code received"
            )

    def _send_success_response(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        html = """
        <html>
        <head><title>Stardag - Login Successful</title></head>
        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
            <h1>Login Successful!</h1>
            <p>You can close this window and return to the terminal.</p>
        </body>
        </html>
        """
        self.wfile.write(html.encode())

    def _send_error_response(self, error: str, description: str):
        self.send_response(400)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        html = f"""
        <html>
        <head><title>Stardag - Login Failed</title></head>
        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
            <h1>Login Failed</h1>
            <p>Error: {error}</p>
            <p>{description}</p>
        </body>
        </html>
        """
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _exchange_code_for_tokens(
    token_endpoint: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
) -> dict:
    """Exchange authorization code for OIDC tokens."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required. Install with: pip install stardag[cli]")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(token_endpoint, data=data)
        response.raise_for_status()
        return response.json()


def _refresh_oidc_token(
    token_endpoint: str,
    refresh_token: str,
    client_id: str,
) -> dict:
    """Refresh OIDC tokens using refresh token."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required. Install with: pip install stardag[cli]")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(token_endpoint, data=data)
        response.raise_for_status()
        return response.json()


def _exchange_for_internal_token(
    api_url: str,
    oidc_token: str,
    org_id: str,
) -> dict:
    """Exchange OIDC token for internal org-scoped token via /auth/exchange."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required. Install with: pip install stardag[cli]")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{api_url}/api/v1/auth/exchange",
            json={"org_id": org_id},
            headers={"Authorization": f"Bearer {oidc_token}"},
        )
        response.raise_for_status()
        return response.json()


def _get_oidc_config(issuer: str) -> dict:
    """Fetch OIDC configuration from issuer."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required. Install with: pip install stardag[cli]")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(f"{issuer}/.well-known/openid-configuration")
        response.raise_for_status()
        return response.json()


def _get_user_organizations(api_url: str, oidc_token: str) -> list[dict]:
    """Fetch user's organizations from API using OIDC token."""
    try:
        import httpx
    except ImportError:
        return []

    try:
        # Use /ui/me which accepts OIDC tokens directly (before org-scoped exchange)
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{api_url}/api/v1/ui/me",
                headers={"Authorization": f"Bearer {oidc_token}"},
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("organizations", [])
    except Exception:
        pass
    return []


def _get_workspaces(api_url: str, access_token: str, org_id: str) -> list[dict]:
    """Fetch workspaces for an organization."""
    try:
        import httpx
    except ImportError:
        return []

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 200:
                return response.json()
    except Exception:
        pass
    return []


def _sync_target_roots(
    api_url: str, access_token: str, org_id: str, workspace_id: str
) -> None:
    """Sync target roots from server."""
    try:
        import httpx
    except ImportError:
        return

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/target-roots",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 200:
                roots = response.json()
                target_roots = {root["name"]: root["uri_prefix"] for root in roots}
                set_target_roots(
                    target_roots,
                    registry_url=api_url,
                    organization_id=org_id,
                    workspace_id=workspace_id,
                )
                if target_roots:
                    typer.echo(f"Synced {len(target_roots)} target root(s)")
    except Exception:
        pass


@app.command()
def login(
    registry: str = typer.Option(
        None,
        "--registry",
        "-r",
        help="Registry name to login to (uses default if not specified)",
    ),
    api_url: str = typer.Option(
        None,
        "--api-url",
        "-u",
        help="Base URL of the Stardag API (overrides registry URL)",
    ),
    oidc_issuer: str = typer.Option(
        DEFAULT_OIDC_ISSUER,
        "--oidc-issuer",
        help="OIDC issuer URL (e.g., Keycloak realm URL)",
    ),
    client_id: str = typer.Option(
        DEFAULT_OIDC_CLIENT_ID,
        "--client-id",
        help="OIDC client ID",
    ),
) -> None:
    """Login to Stardag API via browser.

    Opens your browser to authenticate with the OIDC identity provider.
    After successful login, credentials are stored locally.

    The login flow:
    1. Authenticate via OIDC (OAuth PKCE flow)
    2. Store refresh token for the registry
    3. Fetch user's organizations
    4. Exchange for org-scoped internal token
    5. Create a profile for easy access

    For production/CI, use the STARDAG_API_KEY environment variable instead.
    """
    # Check if API key is already set via env var
    env_api_key = os.environ.get("STARDAG_API_KEY")
    if env_api_key:
        typer.echo("STARDAG_API_KEY environment variable is set.")
        typer.echo("You're already authenticated via API key.")
        typer.echo("")
        typer.echo("To use browser login instead, unset the environment variable:")
        typer.echo("  unset STARDAG_API_KEY")
        return

    # Determine registry and URL
    if api_url:
        # URL provided directly, create/update registry
        if not registry:
            registry = "default"
        add_registry(registry, api_url)
        effective_url = api_url.rstrip("/")
    elif registry:
        # Look up registry URL
        effective_url = get_registry_url(registry)
        if not effective_url:
            typer.echo(f"Error: Registry '{registry}' not found in config", err=True)
            typer.echo("Add it with: stardag config registry add <name> --url <url>")
            raise typer.Exit(1)
    else:
        # Use default
        registry = "local"
        effective_url = DEFAULT_API_URL
        # Ensure local registry exists
        add_registry(registry, effective_url)

    typer.echo(f"Logging into registry: {registry} ({effective_url})")
    typer.echo("Fetching OIDC configuration...")

    try:
        oidc_config = _get_oidc_config(oidc_issuer)
    except Exception as e:
        typer.echo(
            f"Error: Could not fetch OIDC configuration from {oidc_issuer}", err=True
        )
        typer.echo(f"  {e}", err=True)
        typer.echo("")
        typer.echo("Make sure Keycloak is running (docker compose up keycloak)")
        raise typer.Exit(1)

    auth_endpoint = oidc_config["authorization_endpoint"]
    token_endpoint = oidc_config["token_endpoint"]

    # Generate PKCE challenge
    code_verifier, code_challenge = _generate_pkce()

    # Start callback server
    redirect_uri = f"http://localhost:{CALLBACK_PORT}/callback"
    OAuthCallbackHandler.auth_result = None

    server = socketserver.TCPServer(("", CALLBACK_PORT), OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    # Build authorization URL
    state = secrets.token_urlsafe(16)
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",  # Note: offline_access removed for local dev
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"

    typer.echo("")
    typer.echo("Opening browser for authentication...")
    typer.echo(f"If the browser doesn't open, visit: {auth_url}")
    typer.echo("")

    # Open browser
    webbrowser.open(auth_url)

    # Wait for callback
    typer.echo("Waiting for authentication...")
    timeout = 120  # 2 minutes
    start = time.time()

    while OAuthCallbackHandler.auth_result is None and time.time() - start < timeout:
        time.sleep(0.5)

    server.server_close()

    if OAuthCallbackHandler.auth_result is None:
        typer.echo("Error: Authentication timed out", err=True)
        raise typer.Exit(1)

    result = OAuthCallbackHandler.auth_result

    if result.error:
        typer.echo(f"Error: {result.error}", err=True)
        raise typer.Exit(1)

    if not result.code:
        typer.echo("Error: No authorization code received", err=True)
        raise typer.Exit(1)

    typer.echo("Exchanging code for tokens...")

    try:
        tokens = _exchange_code_for_tokens(
            token_endpoint=token_endpoint,
            code=result.code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )
    except Exception as e:
        typer.echo(f"Error exchanging code for tokens: {e}", err=True)
        raise typer.Exit(1)

    # Save credentials (refresh token only - per registry)
    creds_to_save: dict[str, str] = {
        "token_endpoint": token_endpoint,
        "client_id": client_id,
    }
    if tokens.get("refresh_token"):
        creds_to_save["refresh_token"] = tokens["refresh_token"]
    save_credentials(creds_to_save, registry)  # type: ignore[arg-type]

    typer.echo("")
    typer.echo("Login successful!")
    typer.echo(f"Credentials saved to {get_credentials_path(registry)}")

    # Fetch organizations and prompt for selection
    oidc_token = tokens["access_token"]
    organizations = _get_user_organizations(effective_url, oidc_token)

    if not organizations:
        typer.echo("")
        typer.echo("No organizations found.")
        typer.echo("Create one in the web UI or contact your admin.")
        return

    # Select organization
    if len(organizations) == 1:
        org = organizations[0]
        typer.echo(f"Using organization: {org['name']} ({org['slug']})")
    else:
        typer.echo("")
        typer.echo("Select an organization:")
        for i, org in enumerate(organizations):
            typer.echo(f"  {i + 1}. {org['name']} ({org['slug']})")

        choice = typer.prompt("Enter number", type=int, default=1)
        if choice < 1 or choice > len(organizations):
            typer.echo("Invalid selection")
            raise typer.Exit(1)
        org = organizations[choice - 1]

    org_id = org["id"]
    org_slug = org["slug"]

    # Cache org slug -> ID mapping
    cache_org_id(registry, org_slug, org_id)

    # Exchange for internal org-scoped token
    typer.echo(f"Exchanging for org-scoped token ({org_slug})...")
    try:
        internal_tokens = _exchange_for_internal_token(
            effective_url, oidc_token, org_id
        )
        access_token = internal_tokens["access_token"]
        expires_in = internal_tokens.get("expires_in", 600)

        # Cache the access token
        save_access_token_cache(registry, org_id, access_token, expires_in)
        typer.echo("Access token cached")

    except Exception as e:
        typer.echo(f"Warning: Could not get org-scoped token: {e}")
        typer.echo("You may need to manually create a profile")
        return

    # Fetch workspaces
    workspaces = _get_workspaces(effective_url, access_token, org_id)

    if not workspaces:
        typer.echo("No workspaces found in organization")
        return

    # Select workspace
    if len(workspaces) == 1:
        ws = workspaces[0]
        typer.echo(f"Using workspace: {ws['name']} ({ws['slug']})")
    else:
        typer.echo("")
        typer.echo("Select a workspace:")
        for i, ws in enumerate(workspaces):
            personal = " (personal)" if ws.get("owner_id") else ""
            typer.echo(f"  {i + 1}. {ws['name']} ({ws['slug']}){personal}")

        choice = typer.prompt("Enter number", type=int, default=1)
        if choice < 1 or choice > len(workspaces):
            typer.echo("Invalid selection")
            raise typer.Exit(1)
        ws = workspaces[choice - 1]

    workspace_id = ws["id"]
    workspace_slug = ws["slug"]

    # Cache workspace slug -> ID mapping
    cache_workspace_id(registry, org_id, workspace_slug, workspace_id)

    # Sync target roots
    _sync_target_roots(effective_url, access_token, org_id, workspace_id)

    # Create profile with slugs (not IDs) for readability
    profile_name = f"{registry}-{org_slug}-{workspace_slug}"
    add_profile(profile_name, registry, org_slug, workspace_slug)
    typer.echo(f"Created profile: {profile_name}")

    # Set as default if no other profiles
    profiles = list_profiles()
    if len(profiles) == 1:
        set_default_profile(profile_name)
        typer.echo("Set as default profile")

    typer.echo("")
    typer.echo("Setup complete!")
    typer.echo(f"Use this profile with: STARDAG_PROFILE={profile_name}")


@app.command()
def logout(
    registry: str = typer.Option(
        None,
        "--registry",
        "-r",
        help="Registry to logout from (uses current profile's registry if not specified)",
    ),
) -> None:
    """Logout and clear stored credentials."""
    if clear_credentials(registry):
        typer.echo("Credentials cleared successfully.")
    else:
        typer.echo("No credentials found.")


@app.command()
def status(
    registry: str = typer.Option(
        None,
        "--registry",
        "-r",
        help="Registry to check (uses current profile if not specified)",
    ),
) -> None:
    """Show current authentication status and active context."""
    config = get_config()

    # Check env var first
    env_api_key = os.environ.get("STARDAG_API_KEY")
    if env_api_key:
        prefix = env_api_key[:11] if len(env_api_key) > 11 else env_api_key[:4]
        typer.echo("Authentication: API Key (environment variable)")
        typer.echo(f"  STARDAG_API_KEY: {prefix}...")
        typer.echo("")
        typer.echo("Note: API key determines workspace automatically.")
        return

    # Show profile-based status
    profile = config.context.profile
    registry_name = registry or config.context.registry_name

    typer.echo("Configuration:")
    typer.echo(f"  Config file: {get_config_path()}")
    if profile:
        typer.echo(f"  Active profile: {profile}")
    else:
        typer.echo("  Active profile: (none - using defaults)")

    typer.echo("")
    typer.echo("Active Context:")
    typer.echo(f"  Registry: {registry_name or '(not set)'}")
    typer.echo(f"  API URL: {config.api.url}")
    typer.echo(f"  Organization: {config.context.organization_id or '(not set)'}")
    typer.echo(f"  Workspace: {config.context.workspace_id or '(not set)'}")

    typer.echo("")
    typer.echo("Authentication:")

    if not registry_name:
        typer.echo("  Status: Not configured")
        typer.echo("")
        typer.echo("Run 'stardag auth login' to authenticate")
        return

    creds = load_credentials(registry_name)
    if creds is None:
        typer.echo("  Status: Not logged in")
        typer.echo("")
        typer.echo(
            f"Run 'stardag auth login --registry {registry_name}' to authenticate"
        )
        return

    typer.echo("  Status: Logged in (has refresh token)")
    typer.echo(f"  Credentials: {get_credentials_path(registry_name)}")

    # Check for cached access token
    if config.access_token:
        typer.echo(f"  Access token: {config.access_token[:20]}... (cached)")
    else:
        typer.echo("  Access token: (not cached or expired)")

    typer.echo("")
    typer.echo("To switch profiles: STARDAG_PROFILE=<profile-name>")
    typer.echo("To list profiles: stardag profile list")


@app.command()
def refresh(
    registry: str = typer.Option(
        None,
        "--registry",
        "-r",
        help="Registry to refresh token for",
    ),
    org_id: str = typer.Option(
        None,
        "--org",
        "-o",
        help="Organization ID to get token for",
    ),
) -> None:
    """Refresh the access token using stored refresh token."""
    config = get_config()
    registry_name = registry or config.context.registry_name
    organization_id = org_id or config.context.organization_id

    if not registry_name:
        typer.echo("Error: No registry specified and no active profile", err=True)
        raise typer.Exit(1)

    if not organization_id:
        typer.echo("Error: No organization specified", err=True)
        raise typer.Exit(1)

    creds = load_credentials(registry_name)
    if not creds or not creds.get("refresh_token"):
        typer.echo(
            "Error: No refresh token found. Run 'stardag auth login' first.", err=True
        )
        raise typer.Exit(1)

    # Get registry URL
    registry_url = get_registry_url(registry_name)
    if not registry_url:
        typer.echo(f"Error: Registry '{registry_name}' not found", err=True)
        raise typer.Exit(1)

    typer.echo(f"Refreshing token for {registry_name}/{organization_id}...")

    # Refresh OIDC token
    token_endpoint = creds.get("token_endpoint")
    refresh_token = creds.get("refresh_token")
    client_id = creds.get("client_id")

    if not token_endpoint or not refresh_token or not client_id:
        typer.echo(
            "Error: Invalid credentials (missing token_endpoint, refresh_token, or client_id).",
            err=True,
        )
        raise typer.Exit(1)

    try:
        tokens = _refresh_oidc_token(
            token_endpoint,
            refresh_token,
            client_id,
        )

        # Update stored refresh token if a new one was provided
        if tokens.get("refresh_token"):
            creds["refresh_token"] = tokens["refresh_token"]
            save_credentials(creds, registry_name)

    except Exception as e:
        typer.echo(f"Error refreshing OIDC token: {e}", err=True)
        typer.echo("You may need to login again: stardag auth login")
        raise typer.Exit(1)

    # Exchange for internal token
    try:
        internal_tokens = _exchange_for_internal_token(
            registry_url, tokens["access_token"], organization_id
        )
        access_token = internal_tokens["access_token"]
        expires_in = internal_tokens.get("expires_in", 600)

        save_access_token_cache(
            registry_name, organization_id, access_token, expires_in
        )
        typer.echo("Access token refreshed and cached")

    except Exception as e:
        typer.echo(f"Error exchanging for internal token: {e}", err=True)
        raise typer.Exit(1)
