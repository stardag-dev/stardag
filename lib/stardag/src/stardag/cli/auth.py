"""Authentication commands for Stardag CLI.

Supports two authentication modes:
1. API Key (production): Set STARDAG_API_KEY environment variable
2. Browser login (local dev): OAuth flow with Keycloak/Cognito
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
    clear_credentials,
    get_config_path,
    get_credentials_path,
    load_config,
    load_credentials,
    save_credentials,
    set_api_url,
    set_organization_id,
    set_target_roots,
    set_workspace_id,
)

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
    """Exchange authorization code for tokens."""
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


def _auto_select_context(api_url: str, access_token: str) -> None:
    """Auto-select organization and workspace after login.

    - If user has exactly one org, auto-select it
    - After org is selected, auto-select personal workspace if available
    """
    try:
        import httpx
    except ImportError:
        return  # Silent fail if httpx not available

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            # Fetch user profile with organizations
            response = client.get(f"{api_url}/api/v1/ui/me")
            if response.status_code != 200:
                typer.echo("")
                typer.echo("Next steps:")
                typer.echo(
                    "  1. List your organizations:  stardag config list organizations"
                )
                typer.echo(
                    "  2. Set active organization:  stardag config set organization <org-id>"
                )
                return

            data = response.json()
            organizations = data.get("organizations", [])

            if not organizations:
                typer.echo("")
                typer.echo("No organizations found. Create one in the web UI first.")
                return

            # Auto-select org (first one if multiple)
            org = organizations[0]
            set_organization_id(org["id"], org["slug"])
            typer.echo("")
            typer.echo(f"Auto-selected organization: {org['name']} ({org['slug']})")

            # Fetch workspaces and auto-select Default workspace
            org_id = org["id"]
            response = client.get(
                f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces"
            )
            if response.status_code != 200:
                typer.echo("")
                typer.echo(
                    "Set workspace with: stardag config set workspace <workspace-id>"
                )
                return

            workspaces = response.json()

            # Look for "Default" workspace (slug "default", not a personal workspace)
            default_ws = None
            for ws in workspaces:
                if ws.get("slug") == "default" and not ws.get("owner_id"):
                    default_ws = ws
                    break

            if default_ws:
                set_workspace_id(default_ws["id"])
                typer.echo(
                    f"Auto-selected workspace: {default_ws['name']} ({default_ws['slug']})"
                )

                # Sync target roots
                _sync_target_roots_after_login(
                    client, api_url, org_id, default_ws["id"]
                )

            # Show available workspaces and help message
            if workspaces:
                typer.echo("")
                typer.echo("Available workspaces:")
                for ws in workspaces:
                    personal = " (personal)" if ws.get("owner_id") else ""
                    marker = " *" if default_ws and ws["id"] == default_ws["id"] else ""
                    typer.echo(
                        f"  {ws['id']}  {ws['name']} ({ws['slug']}){personal}{marker}"
                    )
                typer.echo("")
                if default_ws:
                    typer.echo("* = active workspace")
                typer.echo(
                    "Switch workspace with: stardag config set workspace <workspace-id-or-slug>"
                )
                if len(organizations) > 1:
                    typer.echo(
                        "Switch organization with: stardag config set organization <org-id-or-slug>"
                    )

    except Exception:
        # Silent fail - user can manually set context
        typer.echo("")
        typer.echo("Next steps:")
        typer.echo("  1. List your organizations:  stardag config list organizations")
        typer.echo(
            "  2. Set active organization:  stardag config set organization <org-id>"
        )


def _sync_target_roots_after_login(
    client, api_url: str, org_id: str, workspace_id: str
) -> None:
    """Sync target roots after login."""
    try:
        response = client.get(
            f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/target-roots"
        )
        if response.status_code == 200:
            roots = response.json()
            target_roots = {root["name"]: root["uri_prefix"] for root in roots}
            set_target_roots(target_roots)
            if target_roots:
                typer.echo(f"Synced {len(target_roots)} target root(s)")
    except Exception:
        pass  # Silent fail


@app.command()
def login(
    api_url: str = typer.Option(
        DEFAULT_API_URL,
        "--api-url",
        "-u",
        help="Base URL of the Stardag API",
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

    Opens your browser to authenticate with the identity provider (Keycloak/Cognito).
    After successful login, credentials are stored locally.

    After login, use 'stardag config' commands to set your active organization
    and workspace.

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
        "scope": "openid profile email",
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

    # Save credentials (OAuth tokens only)
    creds_to_save: dict[str, str] = {
        "access_token": tokens["access_token"],
        "token_endpoint": token_endpoint,
        "client_id": client_id,
    }
    if tokens.get("refresh_token"):
        creds_to_save["refresh_token"] = tokens["refresh_token"]
    save_credentials(creds_to_save)  # type: ignore[arg-type]

    # Save api_url to config
    set_api_url(api_url)

    typer.echo("")
    typer.echo("Login successful!")
    typer.echo(f"Credentials saved to {get_credentials_path()}")

    # Auto-select organization and workspace
    _auto_select_context(api_url, tokens["access_token"])


@app.command()
def logout() -> None:
    """Logout and clear stored credentials."""
    if clear_credentials():
        typer.echo("Credentials cleared successfully.")
    else:
        typer.echo("No credentials found.")


@app.command()
def status() -> None:
    """Show current authentication status and active context."""
    # Check env var first
    env_api_key = os.environ.get("STARDAG_API_KEY")
    if env_api_key:
        prefix = env_api_key[:11] if len(env_api_key) > 11 else env_api_key[:4]
        typer.echo("Authentication: API Key (environment variable)")
        typer.echo(f"  STARDAG_API_KEY: {prefix}...")
        typer.echo("")
        typer.echo("Note: API key determines workspace automatically.")
        return

    creds = load_credentials()

    if creds is None:
        typer.echo("Authentication: Not logged in")
        typer.echo("")
        typer.echo("Options:")
        typer.echo("  1. Set STARDAG_API_KEY environment variable (for production/CI)")
        typer.echo("  2. Run 'stardag auth login' for browser-based login (local dev)")
        raise typer.Exit(1)

    config = load_config()

    typer.echo("Authentication: Browser login (JWT)")
    typer.echo(f"  API URL: {config.get('api_url', 'not set')}")

    access_token = creds.get("access_token", "")
    if access_token:
        typer.echo(f"  Token:   {access_token[:20]}...")
    else:
        typer.echo("  Token:   not set")

    has_refresh = bool(creds.get("refresh_token"))
    typer.echo(f"  Refresh: {'available' if has_refresh else 'not available'}")

    typer.echo("")
    typer.echo("Active Context:")

    org_id = config.get("organization_id")
    workspace_id = config.get("workspace_id")

    if org_id:
        typer.echo(f"  Organization: {org_id}")
    else:
        typer.echo(
            "  Organization: not set (run 'stardag config set organization <id>')"
        )

    if workspace_id:
        typer.echo(f"  Workspace:    {workspace_id}")
    else:
        typer.echo("  Workspace:    not set (run 'stardag config set workspace <id>')")

    typer.echo("")
    typer.echo(f"Credentials file: {get_credentials_path()}")
    typer.echo(f"Config file:      {get_config_path()}")


@app.command()
def configure(
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        "-u",
        help="Update the API URL",
    ),
) -> None:
    """Update configuration without re-authenticating.

    Note: Prefer using 'stardag config set api-url <url>' instead.
    """
    if api_url is None:
        typer.echo("No options provided. Use --api-url to update config.")
        typer.echo("")
        typer.echo("Tip: Use 'stardag config set api-url <url>' for configuration.")
        raise typer.Exit(1)

    set_api_url(api_url)
    typer.echo(f"Updated API URL: {api_url}")
