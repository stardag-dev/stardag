"""Post-deploy "connect" phase for ``stardag self-host``.

Wires a freshly deployed self-hosted Stardag service into a ready-to-use
setup, mirroring the Modal account's structure:

- a **primary Stardag workspace** (named after the shared Modal workspace,
  when there is one) or the user's personal workspace,
- a **primary environment** (``main``) in it,
- an **API key** for that environment pushed as the Modal secret
  ``stardag-api-key`` into the Modal environment where the user's DAGs run,
- a **default target root** (``modalvol://stardag/<workspace-slug>``),
- a **local SDK registry + profile** so ``stardag`` CLI/SDK commands work
  immediately.

Everything is idempotent: existing workspaces/environments/target roots are
matched before anything is created, so the flow can be re-run at any time
(``stardag self-host connect``).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import typer
from rich.console import Console

console = Console()
error_console = Console(stderr=True, style="bold red")

# Stardag environment ensured by the setup ("main", mirroring Modal's
# default environment name).
PRIMARY_ENVIRONMENT_SLUG = "main"

# Name of the Modal secret holding the Stardag API key for DAG execution
# (the conventional name modal integration docs/examples use).
API_KEY_SECRET_NAME = "stardag-api-key"

# Modal volume name used for the default target root.
DEFAULT_TARGET_ROOT_NAME = "default"
DEFAULT_TARGET_ROOT_VOLUME = "stardag"

DEFAULT_REGISTRY_NAME = "selfhosted"
DEFAULT_PROFILE_NAME = "selfhosted"

_LOGIN_ATTEMPTS = 3
_LOGIN_TIMEOUT = 120.0  # first request cold-starts the container + database


def _slugify(name: str) -> str:
    """Server-compatible slug derivation (see stardag_api workspace slugs)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]


def parse_target_root_flag(value: str) -> tuple[str, str]:
    """Parse a ``--target-root name=uri`` flag value."""
    name, sep, uri = value.partition("=")
    if not sep or not name.strip() or not uri.strip():
        error_console.print(
            f"Invalid --target-root {value!r}: expected the form name=uri "
            "(e.g. default=modalvol://stardag/my-workspace)"
        )
        raise typer.Exit(1)
    return name.strip(), uri.strip()


def resolve_primary_workspace(
    explicit: str | None,
    no_primary_workspace: bool,
    modal_shared_workspace: str | None,
    interactive: bool,
) -> str | None:
    """Determine the primary (shared) Stardag workspace name.

    Explicit flags win; otherwise the shared Modal workspace's name is the
    default (confirmed interactively). Personal Modal workspaces get no
    shared Stardag workspace - the user's personal Stardag workspace is
    used instead.
    """
    if no_primary_workspace:
        return None
    if explicit:
        return explicit
    if not modal_shared_workspace:
        return None
    if interactive and not typer.confirm(
        f"Primary Stardag workspace: '{modal_shared_workspace}' "
        "(named after your shared Modal workspace)?",
        default=True,
    ):
        return None
    return modal_shared_workspace


def get_auth_config(
    api_url: str, transport: httpx.BaseTransport | None = None
) -> dict | None:
    """Fetch /auth/config from the deployed service (None if unreachable)."""
    try:
        with httpx.Client(timeout=_LOGIN_TIMEOUT, transport=transport) as client:
            response = client.get(f"{api_url}/api/v1/auth/config")
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


def login_local(
    api_url: str,
    email: str,
    password: str,
    registry_name: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Email/password login; stores the session token and returns it.

    Retries a few times: the first request after a deploy cold-starts the
    container (and wakes the database), which can be slow or transiently
    unavailable.
    """
    from stardag._cli.credentials import save_credentials

    last_error: Exception | None = None
    response = None
    for attempt in range(_LOGIN_ATTEMPTS):
        try:
            with httpx.Client(timeout=_LOGIN_TIMEOUT, transport=transport) as client:
                response = client.post(
                    f"{api_url}/api/v1/auth/login",
                    json={"email": email, "password": password},
                )
            if response.status_code < 500:
                break
            last_error = RuntimeError(
                f"Login failed ({response.status_code}): {response.text}"
            )
        except httpx.HTTPError as e:
            last_error = e
            response = None
        if attempt < _LOGIN_ATTEMPTS - 1:
            console.print("  [dim]Service still starting, retrying...[/dim]")
            time.sleep(3 * (attempt + 1))

    if response is None or response.status_code >= 500:
        error_console.print(f"Could not reach the service to sign in: {last_error}")
        raise typer.Exit(1)
    if response.status_code == 401:
        error_console.print("Invalid email or password.")
        raise typer.Exit(1)
    if response.status_code != 200:
        error_console.print(f"Login failed ({response.status_code}): {response.text}")
        raise typer.Exit(1)

    data = response.json()
    session_token = data["session_token"]
    expires_in = data.get("expires_in", 3600)
    save_credentials(
        {
            "auth_mode": "local",
            "session_token": session_token,
            "session_expires_at": time.time() + expires_in - 30,
        },
        registry_name,
        email,
    )
    return session_token


@dataclass
class ConnectOutcome:
    """What the connect flow ensured - input for the summary panel."""

    api_url: str
    workspace_name: str
    workspace_slug: str
    workspace_is_personal: bool
    workspace_created: bool
    environment_slug: str
    environment_created: bool
    target_root: tuple[str, str] | None  # (name, uri) ensured, None if skipped
    api_key_name: str | None  # None if the Modal secret push failed
    modal_secret_name: str | None
    execution_modal_env: str | None  # None = Modal's default environment
    registry_name: str
    profile_name: str
    user_email: str


def _api_error(response: httpx.Response, action: str) -> typer.Exit:
    error_console.print(f"Failed to {action} ({response.status_code}): {response.text}")
    return typer.Exit(1)


def run_connect(
    api_url: str,
    bearer_token: str,
    user_email: str,
    *,
    primary_workspace: str | None,
    environment_slug: str = PRIMARY_ENVIRONMENT_SLUG,
    execution_modal_env: str | None = None,
    target_root: tuple[str, str] | None = None,
    no_target_root: bool = False,
    registry_name: str = DEFAULT_REGISTRY_NAME,
    profile_name: str = DEFAULT_PROFILE_NAME,
    push_modal_secret: Callable[[str, dict[str, str], str | None], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ConnectOutcome:
    """Idempotently wire up workspace, environment, API key, and SDK config.

    ``bearer_token`` is a session token (local auth mode) or OIDC token -
    both are accepted by the bootstrap endpoints and ``/auth/exchange``.
    ``push_modal_secret`` is injectable for tests; defaults to the Modal
    secret push used by ``stardag modal stardag-api-key create``.
    """
    from stardag._cli.auth import _sync_target_roots
    from stardag._cli.credentials import (
        add_profile,
        add_registry,
        get_active_profile,
        set_default_profile,
    )
    from stardag._cli.modal import _create_stardag_api_key, _push_modal_secret
    from stardag.config.cache import cache_environment_id, cache_workspace_id
    from stardag.registry._auth import save_access_token_cache

    if push_modal_secret is None:
        push_modal_secret = _push_modal_secret

    api_url = api_url.rstrip("/")
    client = httpx.Client(
        timeout=_LOGIN_TIMEOUT,
        transport=transport,
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    try:
        # --- Current user + workspaces ---
        me_response = client.get(f"{api_url}/api/v1/ui/me")
        if me_response.status_code != 200:
            raise _api_error(me_response, "fetch user profile")
        me = me_response.json()
        user_email = me.get("user", {}).get("email") or user_email
        workspaces = me.get("workspaces", [])

        # --- Resolve / create the workspace ---
        workspace_created = False
        if primary_workspace:
            slug = _slugify(primary_workspace)
            workspace = next(
                (
                    ws
                    for ws in workspaces
                    if not ws.get("is_personal")
                    and (ws["name"] == primary_workspace or ws["slug"] == slug)
                ),
                None,
            )
            if workspace is None:
                create_response = client.post(
                    f"{api_url}/api/v1/ui/workspaces",
                    json={
                        "name": primary_workspace,
                        "slug": slug,
                        "initial_environment_name": environment_slug,
                        "initial_environment_slug": environment_slug,
                    },
                )
                if create_response.status_code == 409:
                    error_console.print(
                        f"Workspace slug {slug!r} already exists but you are "
                        "not a member of it. Ask an owner for an invite, or "
                        "pass a different --primary-workspace name."
                    )
                    raise typer.Exit(1)
                if create_response.status_code != 201:
                    raise _api_error(create_response, "create workspace")
                workspace = create_response.json()
                workspace_created = True
                console.print(
                    f"Created workspace [bold]{primary_workspace}[/bold] ({slug})"
                )
            else:
                console.print(
                    f"Using workspace [bold]{workspace['name']}[/bold] "
                    f"({workspace['slug']})"
                )
        else:
            workspace = next((ws for ws in workspaces if ws.get("is_personal")), None)
            if workspace is None:
                error_console.print(
                    "No personal workspace found for this user. Pass "
                    "--primary-workspace <name> to create a shared one."
                )
                raise typer.Exit(1)
            console.print(
                f"Using personal workspace [bold]{workspace['name']}[/bold] "
                f"({workspace['slug']})"
            )

        workspace_id = str(workspace["id"])
        workspace_slug = workspace["slug"]

        # --- Workspace-scoped token (env/API-key/target-root endpoints) ---
        exchange_response = client.post(
            f"{api_url}/api/v1/auth/exchange",
            json={"workspace_id": workspace_id},
        )
        if exchange_response.status_code != 200:
            raise _api_error(exchange_response, "get workspace access token")
        exchange = exchange_response.json()
        access_token = exchange["access_token"]
        expires_in = exchange.get("expires_in", 600)
        ws_client = httpx.Client(
            timeout=_LOGIN_TIMEOUT,
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    finally:
        client.close()

    try:
        # --- Ensure the primary environment ---
        environment_created = False
        envs_url = f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments"
        envs_response = ws_client.get(envs_url)
        if envs_response.status_code != 200:
            raise _api_error(envs_response, "list environments")
        environment = next(
            (e for e in envs_response.json() if e["slug"] == environment_slug), None
        )
        if environment is None:
            create_env_response = ws_client.post(
                envs_url,
                json={"name": environment_slug, "slug": environment_slug},
            )
            if create_env_response.status_code != 201:
                raise _api_error(create_env_response, "create environment")
            environment = create_env_response.json()
            environment_created = True
            console.print(f"Created environment [bold]{environment_slug}[/bold]")
        else:
            console.print(f"Using environment [bold]{environment_slug}[/bold]")
        environment_id = str(environment["id"])

        # --- Ensure a default target root ---
        ensured_target_root: tuple[str, str] | None = None
        if not no_target_root:
            root_name, root_uri = target_root or (
                DEFAULT_TARGET_ROOT_NAME,
                f"modalvol://{DEFAULT_TARGET_ROOT_VOLUME}/{workspace_slug}",
            )
            roots_url = f"{envs_url}/{environment_id}/target-roots"
            roots_response = ws_client.get(roots_url)
            if roots_response.status_code != 200:
                raise _api_error(roots_response, "list target roots")
            existing = next(
                (r for r in roots_response.json() if r["name"] == root_name), None
            )
            if existing is None:
                create_root_response = ws_client.post(
                    roots_url, json={"name": root_name, "uri_prefix": root_uri}
                )
                if create_root_response.status_code != 201:
                    raise _api_error(create_root_response, "create target root")
                console.print(
                    f"Created target root [bold]{root_name}[/bold] -> {root_uri}"
                )
                ensured_target_root = (root_name, root_uri)
            else:
                ensured_target_root = (root_name, existing["uri_prefix"])
                console.print(
                    f"Target root [bold]{root_name}[/bold] exists -> "
                    f"{existing['uri_prefix']}"
                )

        # --- API key -> Modal secret (DAG-execution environment) ---
        api_key_name: str | None = f"modal-{execution_modal_env or 'default'}"
        modal_secret_name: str | None = API_KEY_SECRET_NAME
        full_key, key_prefix = _create_stardag_api_key(
            ws_client, api_url, workspace_id, environment_id, api_key_name
        )
        console.print(
            f'Created Stardag API key "{api_key_name}" (prefix: {key_prefix})'
        )
        try:
            push_modal_secret(
                API_KEY_SECRET_NAME, {"STARDAG_API_KEY": full_key}, execution_modal_env
            )
            modal_env_display = execution_modal_env or "default"
            console.print(
                f"Pushed Modal secret [bold]{API_KEY_SECRET_NAME}[/bold] "
                f"(Modal environment: {modal_env_display})"
            )
        except Exception as e:
            error_console.print(f"Could not push the Modal secret: {e}")
            console.print(
                "[yellow]Create it later with:[/yellow] "
                "stardag modal stardag-api-key create"
                + (f" --modal-env {execution_modal_env}" if execution_modal_env else "")
            )
            api_key_name = None
            modal_secret_name = None

        # --- Local SDK config: registry + profile + caches ---
        add_registry(registry_name, api_url)
        add_profile(
            profile_name, registry_name, workspace_slug, environment_slug, user_email
        )
        cache_workspace_id(registry_name, workspace_slug, workspace_id)
        cache_environment_id(
            registry_name, workspace_id, environment_slug, environment_id
        )
        save_access_token_cache(
            registry_name, workspace_id, access_token, expires_in, user_email
        )
        if get_active_profile()[0] is None:
            set_default_profile(profile_name)
        try:
            _sync_target_roots(api_url, access_token, workspace_id, environment_id)
        except Exception as e:
            # Local cache only - repopulated on the next `stardag auth login`
            console.print(f"[yellow]Could not sync target-root cache: {e}[/yellow]")
        console.print(
            f"Configured local SDK profile [bold]{profile_name}[/bold] "
            f"(registry: {registry_name})"
        )
    finally:
        ws_client.close()

    return ConnectOutcome(
        api_url=api_url,
        workspace_name=workspace["name"],
        workspace_slug=workspace_slug,
        workspace_is_personal=bool(workspace.get("is_personal")),
        workspace_created=workspace_created,
        environment_slug=environment_slug,
        environment_created=environment_created,
        target_root=ensured_target_root,
        api_key_name=api_key_name,
        modal_secret_name=modal_secret_name,
        execution_modal_env=execution_modal_env,
        registry_name=registry_name,
        profile_name=profile_name,
        user_email=user_email,
    )


def print_summary(
    outcome: ConnectOutcome,
    app_name: str,
    server_modal_env: str | None,
) -> None:
    """Render the final 'what exists now' panel."""
    from rich.panel import Panel

    ws_kind = "personal" if outcome.workspace_is_personal else "shared"
    exec_env = outcome.execution_modal_env or "default"
    server_env = server_modal_env or "default"

    lines = [
        "[bold]Server[/bold]",
        f"  URL:          {outcome.api_url}",
        f"  Modal app:    {app_name}  (Modal environment: {server_env})",
        "",
        "[bold]Stardag registry[/bold]",
        f"  Workspace:    {outcome.workspace_name} "
        f"(/{outcome.workspace_slug}, {ws_kind})",
        f"  Environment:  {outcome.environment_slug}",
    ]
    if outcome.target_root:
        name, uri = outcome.target_root
        lines.append(f"  Target root:  {name} -> {uri}")
    if outcome.api_key_name and outcome.modal_secret_name:
        lines.append(
            f"  API key:      {outcome.api_key_name} -> Modal secret "
            f"'{outcome.modal_secret_name}' (Modal environment: {exec_env})"
        )
    lines += [
        "",
        "[bold]Local SDK config[/bold]",
        f"  Registry:     {outcome.registry_name} -> {outcome.api_url}",
        f"  Profile:      {outcome.profile_name} "
        f"({outcome.user_email} / {outcome.workspace_slug} / "
        f"{outcome.environment_slug})",
        "",
        "[bold]Next steps[/bold]",
        f"  1. Open the UI: {outcome.api_url}",
        "  2. Deploy a DAG app to Modal: stardag modal deploy <your_app.py>",
        "     (see the docs: How-to -> Integrate with Modal)",
        "  3. Upgrade later: stardag self-host upgrade",
    ]
    console.print(
        Panel("\n".join(lines), title="Stardag is set up", border_style="green")
    )
