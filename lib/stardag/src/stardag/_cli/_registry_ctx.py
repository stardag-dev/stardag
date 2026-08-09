"""Shared registry-client acquisition for registry-backed CLI groups.

Every command that talks to the registry API needs the same three things:
the ``-p/--stardag-profile`` and ``-e/--stardag-env`` overrides, a client
built for whatever they resolve to, and one uniform way to turn a
:class:`StardagError` into a friendly exit. Those live here so
``concurrency-limits``, ``builds`` and ``tasks`` share one implementation
rather than three copies that drift.

Command modules import these names into their own namespace, which keeps
the established test seam working: patching
``stardag._cli.<module>._resolve_registry`` still intercepts the lookup,
because the import binds a module-level attribute.
"""

import os
from typing import NoReturn

import typer
from rich.console import Console

from stardag.config.loader import clear_config_cache, get_config
from stardag.exceptions import SDKVersionUnsupportedError
from stardag.registry import APIRegistry

console = Console()
error_console = Console(stderr=True)


_PROFILE_OPTION = typer.Option(
    None,
    "-p",
    "--stardag-profile",
    help="Stardag profile to use. Defaults to the active profile.",
)
_ENV_OPTION = typer.Option(
    None,
    "-e",
    "--stardag-env",
    help="Stardag environment slug or ID. Defaults to the active profile's env.",
)


def _resolve_registry(profile: str | None, env_override: str | None) -> APIRegistry:
    """Build an APIRegistry for the (optionally overridden) profile/env.

    Mirrors ``configure_limits.py``'s guard: exits with a clear error when
    no registry is configured. When ``profile`` is given it is applied via
    STARDAG_PROFILE (with a config-cache clear) for the duration of
    construction; the built registry captures its URL/auth/env eagerly, so
    the environment override is restored before returning.
    """
    old_profile = os.environ.get("STARDAG_PROFILE")
    try:
        if profile:
            os.environ["STARDAG_PROFILE"] = profile
            clear_config_cache()

        config = get_config()
        reg = config.registry
        if reg is None or not reg.url:
            error_console.print(
                "[bold red]No registry configured.[/bold red]\n"
                "Run 'stardag auth login', activate a profile with "
                "STARDAG_PROFILE, or set STARDAG_API_URL / STARDAG_API_KEY."
            )
            raise typer.Exit(1)

        environment_id = reg.environment_id or None
        if env_override:
            from stardag.config.cache import _looks_like_uuid

            if _looks_like_uuid(env_override):
                # A raw environment ID needs no registry/workspace context to
                # resolve — use it directly (documented alongside slugs).
                environment_id = env_override
            else:
                from stardag._cli.credentials import resolve_environment_slug_to_id

                registry_name = config.context.registry_name
                workspace_id = reg.workspace_id or None
                user = reg.auth.user_email
                if not registry_name or not workspace_id:
                    error_console.print(
                        "[bold red]Cannot resolve an environment slug without a "
                        "configured registry and workspace.[/bold red] Pass an "
                        "environment ID, or activate a profile."
                    )
                    raise typer.Exit(1)
                resolved = resolve_environment_slug_to_id(
                    registry_name, workspace_id, env_override, user
                )
                if not resolved:
                    error_console.print(
                        f"[bold red]Could not resolve environment: "
                        f"{env_override}[/bold red]"
                    )
                    raise typer.Exit(1)
                environment_id = resolved

        return APIRegistry(environment_id=environment_id)
    finally:
        if old_profile is not None:
            os.environ["STARDAG_PROFILE"] = old_profile
        else:
            os.environ.pop("STARDAG_PROFILE", None)
        if profile:
            clear_config_cache()


def _fail(exc: Exception) -> NoReturn:
    """Print a friendly error for a registry/API failure and exit."""
    if isinstance(exc, SDKVersionUnsupportedError):
        # The one error whose text is authored by the *server*: it knows both
        # versions and the exact upgrade command. Print it on its own line
        # with markup disabled — a version specifier like ``stardag>=1.2``
        # is fine, but rich would silently eat anything bracketed, and this
        # is the one message a user has to be able to copy verbatim.
        error_console.print(
            "[bold red]Error:[/bold red] SDK too old for this registry."
        )
        error_console.print(exc.message, markup=False, highlight=False)
        raise typer.Exit(1)
    error_console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(1)
