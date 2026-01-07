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
import logging
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
    Credentials,
    InvalidProfileError,
    add_profile,
    add_registry,
    clear_credentials,
    find_matching_profile,
    get_access_token,
    get_active_profile,
    get_config_path,
    get_credentials_path,
    get_registry_url,
    list_profiles,
    list_registries,
    list_registries_with_credentials,
    load_credentials,
    resolve_org_slug_to_id,
    resolve_workspace_slug_to_id,
    save_access_token_cache,
    save_credentials,
    set_default_profile,
    set_target_roots,
    validate_active_profile,
)
from stardag.config import (
    _looks_like_uuid,
    cache_org_id,
    cache_workspace_id,
    get_config,
)

logger = logging.getLogger(__name__)

app = typer.Typer(help="Authentication commands for Stardag API")


def _validate_active_profile_cli() -> tuple[str, str] | tuple[None, None]:
    """Validate active profile and exit with error if invalid.

    Wrapper around validate_active_profile() that handles CLI error output.
    """
    try:
        return validate_active_profile()
    except InvalidProfileError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


# Default OIDC configuration for local development (Keycloak)
LOCAL_OIDC_ISSUER = "http://localhost:8080/realms/stardag"
LOCAL_OIDC_CLIENT_ID = "stardag-sdk"
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


def _extract_user_from_oidc_token(token: str) -> str | None:
    """Extract user email from OIDC access token (JWT) for local use only.

    This function extracts the user identifier (email) from a JWT token that
    was just received from a trusted OIDC provider. Signature verification is
    intentionally skipped because:
    1. The token was obtained directly from the OIDC token endpoint over HTTPS
    2. This is only used locally to determine credential storage paths
    3. The token will still be validated by the backend when used

    WARNING: Do not use this function to validate tokens or make authorization
    decisions. It only extracts claims for local convenience (e.g., determining
    which credential file to use).

    Args:
        token: OIDC access token (JWT format: header.payload.signature).

    Returns:
        User email if found in token claims ('email' or 'preferred_username'),
        None if extraction fails.
    """
    import base64
    import json

    try:
        # JWT format: header.payload.signature
        parts = token.split(".")
        if len(parts) != 3:
            logger.debug("Invalid JWT format: expected 3 parts, got %d", len(parts))
            return None

        # Decode payload (add base64 padding if needed)
        payload = parts[1]
        missing_padding = (4 - len(payload) % 4) % 4
        payload += "=" * missing_padding

        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)

        # Try common claim names for email
        return claims.get("email") or claims.get("preferred_username")
    except Exception as e:
        logger.debug("Failed to extract user from OIDC token: %s", e)
        return None


def _get_auth_config_from_registry(api_url: str) -> dict | None:
    """Fetch OIDC configuration from registry server.

    Returns dict with 'oidc_issuer' and 'oidc_client_id', or None if unavailable.
    """
    try:
        import httpx
    except ImportError:
        return None

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{api_url}/api/v1/auth/config")
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


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


def _determine_registry(
    registry_arg: str | None, api_url_arg: str | None
) -> tuple[str, str]:
    """Determine which registry to use for login.

    Priority:
    1. --api-url argument (creates/updates registry)
    2. --registry argument
    3. Active profile's registry (from STARDAG_PROFILE or default)
    4. Prompt user if multiple registries exist
    5. Fall back to "local" with default URL

    Returns:
        Tuple of (registry_name, registry_url)
    """
    # 1. URL provided directly
    if api_url_arg:
        registry_name = registry_arg or "default"
        add_registry(registry_name, api_url_arg)
        return registry_name, api_url_arg.rstrip("/")

    # 2. Registry name provided
    if registry_arg:
        effective_url = get_registry_url(registry_arg)
        if not effective_url:
            typer.echo(
                f"Error: Registry '{registry_arg}' not found in config", err=True
            )
            typer.echo("Add it with: stardag config registry add <name> --url <url>")
            raise typer.Exit(1)
        return registry_arg, effective_url

    # 3. Check active profile (with validation)
    active_profile, _ = _validate_active_profile_cli()

    if active_profile:
        profiles = list_profiles()
        profile_registry = profiles[active_profile]["registry"]
        effective_url = get_registry_url(profile_registry)
        if effective_url:
            return profile_registry, effective_url

    # 4. Check available registries
    registries = list_registries()
    if len(registries) == 0:
        # No registries configured, prompt user to select
        typer.echo("No registry configured. Select one:")
        typer.echo("")
        typer.echo("  1. Stardag Cloud (https://api.stardag.com)")
        typer.echo("  2. Local registry (http://localhost:8000)")
        typer.echo("  3. Other (enter URL)")
        typer.echo("")

        choice = typer.prompt("Enter number", type=int, default=1)
        if choice == 1:
            add_registry("cloud", "https://api.stardag.com")
            return "cloud", "https://api.stardag.com"
        elif choice == 2:
            add_registry("local", DEFAULT_API_URL)
            return "local", DEFAULT_API_URL
        elif choice == 3:
            custom_url = typer.prompt("Enter registry URL")
            custom_url = custom_url.rstrip("/")
            registry_name = typer.prompt(
                "Enter a name for this registry", default="custom"
            )
            add_registry(registry_name, custom_url)
            return registry_name, custom_url
        else:
            typer.echo("Invalid selection, defaulting to Stardag Cloud")
            add_registry("cloud", "https://api.stardag.com")
            return "cloud", "https://api.stardag.com"
    elif len(registries) == 1:
        # Only one registry, use it
        registry_name = list(registries.keys())[0]
        return registry_name, registries[registry_name]
    else:
        # Multiple registries, prompt user
        typer.echo("Multiple registries configured. Select one:")
        typer.echo("")
        registry_names = list(registries.keys())
        for i, name in enumerate(registry_names):
            typer.echo(f"  {i + 1}. {name} ({registries[name]})")
        typer.echo("")
        typer.echo("Hint: Use --registry <name> to skip this prompt,")
        typer.echo(
            "  or set a default profile with: stardag config profile use <profile>,\n"
            "  or set an active profile with: export STARDAG_PROFILE=<profile>"
        )
        typer.echo("")

        choice = typer.prompt("Enter number", type=int, default=1)
        if choice < 1 or choice > len(registry_names):
            typer.echo("Invalid selection")
            raise typer.Exit(1)
        registry_name = registry_names[choice - 1]
        return registry_name, registries[registry_name]


@app.command()
def login(
    registry: str = typer.Option(
        None,
        "--registry",
        "-r",
        help="Registry name to login to (uses active profile's registry if not specified)",
    ),
    api_url: str = typer.Option(
        None,
        "--api-url",
        "-u",
        help="Base URL of the Stardag API (overrides registry URL)",
    ),
    oidc_issuer: str = typer.Option(
        None,
        "--oidc-issuer",
        help="OIDC issuer URL (overrides registry config)",
    ),
    client_id: str = typer.Option(
        None,
        "--client-id",
        help="OIDC client ID (overrides registry config)",
    ),
) -> None:
    """Login to Stardag API via browser.

    Opens your browser to authenticate with the OIDC identity provider.
    After successful login, credentials are stored locally.

    If a profile is active (via STARDAG_PROFILE or default), this command
    will refresh credentials for that profile's registry without prompting
    for organization/workspace selection.

    For first-time setup (no profiles), you'll be guided through org/workspace
    selection to create your first profile.

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

    # Determine which registry to use
    effective_registry, effective_url = _determine_registry(registry, api_url)

    # Check if we have an active profile for this registry
    active_profile, active_source = get_active_profile()
    profiles = list_profiles()
    has_active_profile_for_registry = (
        active_profile
        and active_profile in profiles
        and profiles[active_profile]["registry"] == effective_registry
    )

    typer.echo(f"Logging into registry: {effective_registry} ({effective_url})")

    # Determine OIDC issuer and client ID
    # Priority: CLI args > registry config > local defaults
    effective_issuer = oidc_issuer
    effective_client_id = client_id

    if not effective_issuer or not effective_client_id:
        typer.echo("Fetching auth configuration from registry...")
        auth_config = _get_auth_config_from_registry(effective_url)

        if auth_config:
            if not effective_issuer:
                effective_issuer = auth_config.get("oidc_issuer")
            if not effective_client_id:
                effective_client_id = auth_config.get("oidc_client_id")
            typer.echo(f"Using OIDC issuer: {effective_issuer}")
        else:
            # Fall back to local defaults only if registry is "local"
            if effective_registry == "local":
                typer.echo("Using local development defaults")
                if not effective_issuer:
                    effective_issuer = LOCAL_OIDC_ISSUER
                if not effective_client_id:
                    effective_client_id = LOCAL_OIDC_CLIENT_ID
            else:
                typer.echo(
                    f"Error: Could not fetch auth config from {effective_url}",
                    err=True,
                )
                typer.echo(
                    "The registry server may not support the /auth/config endpoint.",
                    err=True,
                )
                typer.echo("")
                typer.echo("You can manually specify OIDC settings:")
                typer.echo(
                    f"  stardag auth login -r {effective_registry} "
                    "--oidc-issuer <issuer-url> --client-id <client-id>"
                )
                raise typer.Exit(1)

    # Ensure we have both values
    if not effective_issuer or not effective_client_id:
        typer.echo("Error: Could not determine OIDC configuration", err=True)
        raise typer.Exit(1)

    typer.echo("Fetching OIDC configuration...")

    try:
        oidc_config = _get_oidc_config(effective_issuer)
    except Exception as e:
        typer.echo(
            f"Error: Could not fetch OIDC configuration from {effective_issuer}",
            err=True,
        )
        typer.echo(f"  {e}", err=True)
        typer.echo("")
        if effective_registry == "local":
            typer.echo("Make sure Keycloak is running (docker compose up keycloak)")
        else:
            typer.echo("Check that the OIDC issuer is accessible.")
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
        "client_id": effective_client_id,
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
            client_id=effective_client_id,
        )
    except Exception as e:
        typer.echo(f"Error exchanging code for tokens: {e}", err=True)
        raise typer.Exit(1)

    # Extract user email from tokens
    # Try ID token first (per OIDC spec, user claims like email are in id_token)
    # Fall back to access token (some providers put claims there too)
    oidc_token = tokens["access_token"]
    logged_in_user: str | None = None
    if tokens.get("id_token"):
        logged_in_user = _extract_user_from_oidc_token(tokens["id_token"])
    if not logged_in_user:
        logged_in_user = _extract_user_from_oidc_token(oidc_token)

    if not logged_in_user:
        typer.echo(
            "Error: Could not extract user email from tokens. "
            "The OIDC provider may not be returning the email claim.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Logged in as: {logged_in_user}")

    # Save credentials (refresh token - per registry/user)
    creds_to_save: Credentials = {
        "token_endpoint": token_endpoint,
        "client_id": effective_client_id,
    }
    if tokens.get("refresh_token"):
        creds_to_save["refresh_token"] = tokens["refresh_token"]
    save_credentials(creds_to_save, effective_registry, logged_in_user)

    typer.echo("")
    typer.echo("Login successful!")
    typer.echo(
        f"Credentials saved to {get_credentials_path(effective_registry, logged_in_user)}"
    )

    # If we have an active profile for this registry, refresh its token
    if has_active_profile_for_registry and active_profile:
        profile_details = profiles[active_profile]
        profile_user = profile_details.get("user")
        org_slug = profile_details["organization"]
        workspace_slug = profile_details["workspace"]

        typer.echo("")
        typer.echo(f"Active profile: {active_profile}")
        if profile_user:
            typer.echo(f"  User: {profile_user}")
        typer.echo(f"  Organization: {org_slug}")
        typer.echo(f"  Workspace: {workspace_slug}")

        # Warn if logged-in user doesn't match profile user
        if profile_user and logged_in_user and profile_user != logged_in_user:
            typer.echo("")
            typer.echo(
                f"Warning: Logged in as '{logged_in_user}' but profile expects "
                f"'{profile_user}'"
            )
            typer.echo("You may want to update the profile or create a new one")

        # Use logged-in user for caching (profile user if different will use its own cache)
        cache_user = logged_in_user

        # Resolve org slug to ID and cache access token
        # Pass oidc_token since we have it fresh
        org_id = resolve_org_slug_to_id(
            effective_registry, org_slug, oidc_token=oidc_token
        )

        if not org_id:
            # Need to fetch org ID from API
            organizations = _get_user_organizations(effective_url, oidc_token)
            matching_org = next(
                (o for o in organizations if o["slug"] == org_slug), None
            )
            if matching_org:
                org_id = matching_org["id"]
                cache_org_id(effective_registry, org_slug, org_id)
            else:
                typer.echo("")
                typer.echo(
                    f"Warning: Could not find organization '{org_slug}' for this user"
                )
                typer.echo("You may need to update your profile or check org access")

        if org_id:
            # Exchange for internal org-scoped token
            typer.echo(f"Caching access token for {org_slug}...")
            try:
                internal_tokens = _exchange_for_internal_token(
                    effective_url, oidc_token, org_id
                )
                access_token = internal_tokens["access_token"]
                expires_in = internal_tokens.get("expires_in", 600)
                save_access_token_cache(
                    effective_registry, org_id, access_token, expires_in, cache_user
                )
                typer.echo("Access token cached")

                # Also resolve and cache workspace ID if needed
                # Pass access_token since we have it fresh
                workspace_id = resolve_workspace_slug_to_id(
                    effective_registry,
                    org_id,
                    workspace_slug,
                    access_token=access_token,
                )
                if not workspace_id or not _looks_like_uuid(workspace_id):
                    workspaces = _get_workspaces(effective_url, access_token, org_id)
                    matching_ws = next(
                        (w for w in workspaces if w["slug"] == workspace_slug), None
                    )
                    if matching_ws:
                        workspace_id = matching_ws["id"]
                        cache_workspace_id(
                            effective_registry, org_id, workspace_slug, workspace_id
                        )

                # Sync target roots
                if workspace_id:
                    _sync_target_roots(
                        effective_url, access_token, org_id, workspace_id
                    )

            except Exception as e:
                typer.echo(f"Warning: Could not cache access token: {e}")
                typer.echo("Run 'stardag auth refresh' to try again")

        if active_source == "env":
            typer.echo("")
            typer.echo(f"(Active via STARDAG_PROFILE={active_profile})")
        else:
            typer.echo("")
            typer.echo("(Active via [default] in config)")

        typer.echo("")
        typer.echo("To switch profiles:")
        typer.echo("  - Set env var: export STARDAG_PROFILE=<profile-name>")
        typer.echo("  - Or set default: stardag config profile use <profile-name>")
        typer.echo("  - List profiles: stardag config profile list")
        return

    # Check if any profiles exist
    if profiles:
        # Profiles exist but none active for this registry
        # Show existing profiles and how to use/create them
        typer.echo("")
        typer.echo("Existing profiles:")
        for name, details in profiles.items():
            typer.echo(f"  - {name} (registry: {details['registry']})")

        typer.echo("")
        typer.echo("To activate an existing profile:")
        typer.echo("  export STARDAG_PROFILE=<profile-name>")
        typer.echo("  # or: stardag config profile use <profile-name>")
        typer.echo("")
        typer.echo("To create a new profile:")
        typer.echo(
            "  stardag config profile add <name> "
            f"-r {effective_registry} -o <org-slug> -w <workspace-slug>"
        )
        return

    # First-time setup (no profiles exist)
    # Fetch organizations and prompt for selection
    # Note: oidc_token and logged_in_user are already extracted and validated above
    assert logged_in_user is not None  # Validated earlier with exit
    organizations = _get_user_organizations(effective_url, oidc_token)

    if not organizations:
        typer.echo("")
        typer.echo("No organizations found.")
        typer.echo("Create one in the web UI or contact your admin.")
        return

    # Select organization
    if len(organizations) == 1:
        org = organizations[0]
        typer.echo(f'Using organization: "{org["name"]}" (/{org["slug"]})')
    else:
        typer.echo("")
        typer.echo("Select an organization:")
        for i, org in enumerate(organizations):
            typer.echo(f'  {i + 1}. "{org["name"]}" (/{org["slug"]})')

        choice = typer.prompt("Enter number", type=int, default=1)
        if choice < 1 or choice > len(organizations):
            typer.echo("Invalid selection")
            raise typer.Exit(1)
        org = organizations[choice - 1]

    org_id = org["id"]
    org_slug = org["slug"]

    # Cache org slug -> ID mapping
    cache_org_id(effective_registry, org_slug, org_id)

    # Exchange for internal org-scoped token
    typer.echo(f"Exchanging for org-scoped token ({org_slug})...")
    try:
        internal_tokens = _exchange_for_internal_token(
            effective_url, oidc_token, org_id
        )
        access_token = internal_tokens["access_token"]
        expires_in = internal_tokens.get("expires_in", 600)

        # Cache the access token (with user for multi-user support)
        save_access_token_cache(
            effective_registry, org_id, access_token, expires_in, logged_in_user
        )
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
        typer.echo(f'Using workspace: "{ws["name"]}" (/{ws["slug"]})')
    else:
        typer.echo("")
        typer.echo("Select a workspace:")
        for i, ws in enumerate(workspaces):
            typer.echo(f'  {i + 1}. "{ws["name"]}" (/{ws["slug"]})')

        choice = typer.prompt("Enter number", type=int, default=1)
        if choice < 1 or choice > len(workspaces):
            typer.echo("Invalid selection")
            raise typer.Exit(1)
        ws = workspaces[choice - 1]

    workspace_id = ws["id"]
    workspace_slug = ws["slug"]

    # Cache workspace slug -> ID mapping
    cache_workspace_id(effective_registry, org_id, workspace_slug, workspace_id)

    # Sync target roots
    _sync_target_roots(effective_url, access_token, org_id, workspace_id)

    # Check if a matching profile already exists (including user for multi-user)
    existing_profile = find_matching_profile(
        effective_registry, org_slug, workspace_slug, logged_in_user
    )

    if existing_profile:
        # Profile with identical settings already exists
        typer.echo("")
        typer.echo(f"Profile '{existing_profile}' already exists with these settings.")

        # Set as default if it's the only profile or if no default
        if len(profiles) == 0 or get_active_profile()[0] is None:
            set_default_profile(existing_profile)
            typer.echo("Set as default profile")

        typer.echo("")
        typer.echo("Setup complete!")
        typer.echo(f"Use this profile with: STARDAG_PROFILE={existing_profile}")
    else:
        # Create new profile with slugs (not IDs) for readability
        # Include user in profile name if set (for multi-user scenarios)
        if logged_in_user:
            # Use a short version of email for profile name
            # Handle edge case where email might not contain "@"
            email_parts = logged_in_user.split("@")
            user_part = email_parts[0]
            domain_part = email_parts[1] if len(email_parts) > 1 else None
            base_profile_name = (
                f"{effective_registry}-{user_part}-{org_slug}-{workspace_slug}"
            )
            profile_name = base_profile_name

            # Handle collision: if profile name exists with a different user,
            # add domain part to disambiguate (e.g., john@work.com vs john@personal.com)
            if profile_name in profiles:
                existing_user = profiles[profile_name].get("user")
                if existing_user != logged_in_user:
                    if domain_part:
                        # Use domain to disambiguate
                        domain_slug = domain_part.split(".")[
                            0
                        ]  # e.g., "work" from "work.com"
                        profile_name = f"{base_profile_name}-{domain_slug}"
                    # If still collision, add numeric suffix
                    if profile_name in profiles:
                        suffix = 2
                        while f"{profile_name}-{suffix}" in profiles:
                            suffix += 1
                        profile_name = f"{profile_name}-{suffix}"
        else:
            profile_name = f"{effective_registry}-{org_slug}-{workspace_slug}"
        add_profile(
            profile_name, effective_registry, org_slug, workspace_slug, logged_in_user
        )
        typer.echo(f"Created profile: {profile_name}")
        if logged_in_user:
            typer.echo(f"  User: {logged_in_user}")

        # Set as default if no other profiles
        if len(profiles) == 0:
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
    all_registries: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Logout from all registries",
    ),
) -> None:
    """Logout and clear stored credentials."""
    # Handle --all flag
    if all_registries:
        registries_with_creds = list_registries_with_credentials()
        if not registries_with_creds:
            typer.echo("No credentials found.")
            return

        for reg in registries_with_creds:
            clear_credentials(reg)
            typer.echo(f"Cleared credentials for: {reg}")

        typer.echo("")
        typer.echo("Logged out from all registries.")
        return

    # Determine which registry to logout from
    effective_registry = registry

    if not effective_registry:
        # Check active profile first (with validation)
        active_profile, _ = _validate_active_profile_cli()

        if active_profile:
            profiles = list_profiles()
            effective_registry = profiles[active_profile]["registry"]

    if not effective_registry:
        # No active profile, check which registries have credentials
        registries_with_creds = list_registries_with_credentials()

        if not registries_with_creds:
            typer.echo("No credentials found.")
            return

        if len(registries_with_creds) == 1:
            effective_registry = registries_with_creds[0]
        else:
            # Multiple registries with credentials, prompt user
            typer.echo("Multiple registries have stored credentials:")
            typer.echo("")
            for i, reg in enumerate(registries_with_creds):
                typer.echo(f"  {i + 1}. {reg}")
            typer.echo(f"  {len(registries_with_creds) + 1}. All registries")
            typer.echo("")

            choice = typer.prompt("Select registry to logout from", type=int, default=1)
            if choice < 1 or choice > len(registries_with_creds) + 1:
                typer.echo("Invalid selection")
                raise typer.Exit(1)

            if choice == len(registries_with_creds) + 1:
                # Logout from all
                for reg in registries_with_creds:
                    clear_credentials(reg)
                    typer.echo(f"Cleared credentials for: {reg}")
                typer.echo("")
                typer.echo("Logged out from all registries.")
                return

            effective_registry = registries_with_creds[choice - 1]

    # Clear credentials for the selected registry
    if clear_credentials(effective_registry):
        typer.echo(f"Credentials cleared for: {effective_registry}")
    else:
        typer.echo(f"No credentials found for: {effective_registry}")

    # Show remaining credentials
    remaining = list_registries_with_credentials()
    if remaining:
        typer.echo("")
        typer.echo("Still logged in to:")
        for reg in remaining:
            typer.echo(f"  - {reg}")


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
    # Check env var first (before profile validation)
    env_api_key = os.environ.get("STARDAG_API_KEY")
    if env_api_key:
        prefix = env_api_key[:11] if len(env_api_key) > 11 else env_api_key[:4]
        typer.echo("Authentication: API Key (environment variable)")
        typer.echo(f"  STARDAG_API_KEY: {prefix}...")
        typer.echo("")
        typer.echo("Note: API key determines workspace automatically.")
        return

    # Validate active profile if set
    active_profile, active_source = _validate_active_profile_cli()
    profiles = list_profiles()

    typer.echo("Configuration:")
    typer.echo(f"  Config file: {get_config_path()}")

    if active_profile:
        if active_source == "env":
            typer.echo(f"  Active profile: {active_profile} (via STARDAG_PROFILE)")
        else:
            typer.echo(f"  Active profile: {active_profile} (via [default] in config)")

        profile_details = profiles[active_profile]
        registry_name = registry or profile_details["registry"]
        registry_url = get_registry_url(registry_name)
        user = profile_details["user"]

        typer.echo("")
        typer.echo("Active Context:")
        typer.echo(f"  Registry: {registry_name}")
        if registry_url:
            typer.echo(f"  API URL: {registry_url}")
        typer.echo(f"  User: {user}")
        typer.echo(f"  Organization: {profile_details['organization']}")
        typer.echo(f"  Workspace: {profile_details['workspace']}")

        typer.echo("")
        typer.echo("Authentication:")

        if not user:
            typer.echo("  Status: Profile has no user set")
            typer.echo("")
            typer.echo("Run 'stardag auth login' to authenticate")
        else:
            creds = load_credentials(registry_name, user)
            if creds:
                typer.echo(f"  Status: Logged in as {user}")
                typer.echo(
                    f"  Credentials: {get_credentials_path(registry_name, user)}"
                )

                # Check for cached access token
                # Need to resolve org slug to ID to check token cache
                org_slug = profile_details["organization"]
                org_id = resolve_org_slug_to_id(registry_name, org_slug, user)
                access_token = (
                    get_access_token(registry_name, org_id, user) if org_id else None
                )
                if access_token:
                    typer.echo(f"  Access token: {access_token[:20]}... (cached)")
                else:
                    typer.echo("  Access token: (not cached or expired)")
            else:
                typer.echo(f"  Status: Not logged in as {user}")
                typer.echo("")
                typer.echo("Run 'stardag auth login' to authenticate")

        typer.echo("")
        typer.echo("To switch profiles: export STARDAG_PROFILE=<name>")
        typer.echo("To list profiles: stardag config profile list")

    else:
        # No active profile
        typer.echo("  Active profile: (none)")

        typer.echo("")
        typer.echo("Authentication:")

        # Show all registries with credentials
        registries_with_creds = list_registries_with_credentials()

        if registries_with_creds:
            typer.echo("  Logged in to:")
            for reg in registries_with_creds:
                reg_url = get_registry_url(reg)
                if reg_url:
                    typer.echo(f"    - {reg} ({reg_url})")
                else:
                    typer.echo(f"    - {reg}")
        else:
            typer.echo("  Status: Not logged in to any registry")
            typer.echo("")
            typer.echo("Run 'stardag auth login' to authenticate")
            return

        typer.echo("")
        if profiles:
            typer.echo("To activate a profile:")
            typer.echo("  export STARDAG_PROFILE=<name>")
            typer.echo("  # or: stardag config profile use <name>")
            typer.echo("")
            typer.echo("Available profiles:")
            for name in profiles:
                typer.echo(f"  - {name}")
        else:
            typer.echo("No profiles configured.")
            typer.echo("")
            typer.echo("To create a profile:")
            typer.echo(
                "  stardag config profile add <name> -r <registry> -o <org> -w <workspace>"
            )
            typer.echo("  # or run: stardag auth login (for first-time setup)")


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
    # Validate active profile if we're going to use it
    if not registry or not org_id:
        _validate_active_profile_cli()

    config = get_config()
    registry_name = registry or config.context.registry_name
    organization_id = org_id or config.context.organization_id
    user = config.context.user  # Get user from active profile

    if not registry_name:
        typer.echo("Error: No registry specified and no active profile", err=True)
        raise typer.Exit(1)

    if not organization_id:
        typer.echo("Error: No organization specified", err=True)
        raise typer.Exit(1)

    if not user:
        typer.echo(
            "Error: No user in active profile. "
            "Run 'stardag auth login' to authenticate.",
            err=True,
        )
        raise typer.Exit(1)

    # Load credentials
    creds = load_credentials(registry_name, user)
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

    typer.echo(f"Refreshing token for {registry_name}/{user}/{organization_id}...")

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
            save_credentials(creds, registry_name, user)

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
            registry_name, organization_id, access_token, expires_in, user
        )
        typer.echo("Access token refreshed and cached")

    except Exception as e:
        typer.echo(f"Error exchanging for internal token: {e}", err=True)
        raise typer.Exit(1)
