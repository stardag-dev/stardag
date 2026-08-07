from typing import Annotated, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag_api.limits import LimitsSettings


class Settings(BaseSettings):
    # Database configuration - can use either database_url or individual params
    database_url: str | None = None
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "stardag"
    database_user: str = "stardag"
    database_password: str = "stardag"
    # Direct (non-pooled) database URL. When database_url points at a
    # connection pooler (e.g. Neon's PgBouncer endpoint), operations that
    # need a real session - Alembic migrations in particular - must bypass
    # it. Falls back to the regular URL when unset.
    database_url_direct: str | None = None
    # Enable when database_url goes through a transaction-mode pooler
    # (PgBouncer et al.): disables asyncpg's prepared-statement caching,
    # which breaks when consecutive statements may hit different backend
    # sessions.
    database_pooler_compat: bool = False

    debug: bool = False

    # CORS origins (comma-separated)
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # How many reactive scheduler tick summaries to retain per build.
    # Older ones are pruned on insert (see routes/tick_summaries.py). Not
    # a SaaS guardrail (those live in LimitsSettings and default to
    # "unlimited") — an always-on retention window, since the point of
    # the table is a bounded trail, and the useful window is the recent
    # past: a stalled build repeats the same outcome forever.
    max_tick_summaries_per_build: Annotated[int, Field(ge=1)] = 50

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_")

    @property
    def effective_database_url(self) -> str:
        """Get database URL, constructing from individual params if not set."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def effective_migration_database_url(self) -> str:
        """Database URL for migrations: direct (non-pooled) when configured."""
        return self.database_url_direct or self.effective_database_url

    @property
    def cors_origins_list(self) -> list[str]:
        """Get list of allowed CORS origins."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class ReaperSettings(BaseSettings):
    """Optional in-process sweep that cancels abandoned RUNNING builds.

    Same operation as ``POST /builds/bulk-cancel`` with ``idle_for_seconds``
    — the endpoint is the supported, auditable way to drive it (a CLI
    ``builds cleanup`` sits on top). This is for deployments that want it to
    happen unattended, with nothing scheduled outside the API process.

    **Off by default**, and deliberately so: a reaper cancels other people's
    work, and whether a build quiet for N hours is abandoned or merely slow
    is a judgement only the operator of that environment can make. Turn it
    on once you have run the endpoint with ``dry_run`` and agree with what
    it selects.

    **Multi-replica caveat.** Every replica runs its own timer; there is no
    leader election. Cancelling an already-terminal build is a no-op, so
    concurrent sweeps are wasteful, not wrong — they duplicate the scan and
    race harmlessly on the same rows. With more than a couple of replicas,
    prefer an external scheduler calling the endpoint once.
    """

    enabled: bool = False
    # Seconds between sweeps. The first runs one interval after startup, so
    # a crash-looping process never reaps.
    interval_seconds: int = 900
    # A build with no activity for this long is considered abandoned. The
    # default is deliberately generous: a day of complete silence is hard to
    # explain for a live build, and the cost of reaping too eagerly (killing
    # real work) is far higher than reaping late.
    idle_for_seconds: int = 24 * 60 * 60
    # Include reactive builds. Off: they are quiet between ticks by design
    # and have their own watchdog.
    include_reactive: bool = False
    # Also release the claims the reaped builds hold. On — the whole point.
    cascade: bool = True
    # Builds cancelled per sweep, across all environments. Bounds the write
    # set of a single transaction; a backlog drains over successive sweeps.
    max_builds_per_sweep: int = 100

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_REAPER_")


# Bounds on a claim TTL, shared by the ``claim_ttl_seconds`` query parameter
# and by :class:`ClaimSettings`. Both ends reject values that can only be
# mistakes:
#
# - Below a minute a claim can expire while its own executor is still
#   starting up, and one clock-skewed client would hand the task to a second
#   claimant *while the first is running it*. A claim is not a heartbeat
#   lease; there is nothing that renews it mid-execution.
# - Above a month the expiry stops being liveness evidence at all — it is
#   indistinguishable from the "forever" it replaces, and NULL already says
#   that more honestly.
MIN_CLAIM_TTL_SECONDS = 60
MAX_CLAIM_TTL_SECONDS = 30 * 24 * 60 * 60


class ClaimSettings(BaseSettings):
    """Expiry of the per-task execution claim (``Task.latest_status_expires_at``).

    A task whose ``latest_status`` is RUNNING holds the environment-global
    execution claim (see ``services.claims`` for the predicates).
    Recording *when that claim stops being believable* is what lets a third
    party — another build, the concurrency-limit counter — decide the holder
    is gone without probing anything.

    The TTL is written once, at claim time, and is **not** renewed by a
    heartbeat. Callers should therefore pass ``claim_ttl_seconds`` on the
    start, derived from their executor's own timeout plus a small grace:
    the caller is the only party that knows how long the execution it is
    about to spawn can legitimately take.
    """

    # Used when the claiming start supplies no TTL of its own.
    #
    # Deliberately generous. The two failure modes are not symmetric: expiring
    # late merely delays the self-heal of a task that is already wedged today
    # (the status quo is "never"), whereas expiring early hands a *live*
    # task to a second claimant — a double execution, which is the one thing
    # the claim exists to prevent. A day also matches
    # ``ReaperSettings.idle_for_seconds``, so an operator has a single number
    # in their head for "how long silence means abandoned", and it is at or
    # above the maximum function timeout of the execution backends stardag
    # currently drives. Anything longer-running than that must state its own
    # TTL.
    default_ttl_seconds: int = 24 * 60 * 60

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_CLAIM_")


class JWTSettings(BaseSettings):
    """Settings for internal JWT signing and validation."""

    # RSA private key in PEM format (for signing)
    # Generate with: openssl genrsa -out private.pem 2048
    private_key: str | None = None
    # RSA public key in PEM format (for validation, auto-derived if not set)
    public_key: str | None = None
    # Issuer claim for internal tokens
    issuer: str = "stardag-api"
    # Audience claim for internal tokens
    audience: str = "stardag"
    # Access token TTL in minutes
    access_token_ttl_minutes: int = 10
    # Key ID for JWKS (auto-generated if not set)
    key_id: str | None = None

    model_config = SettingsConfigDict(env_prefix="JWT_")


class EmailSettings(BaseSettings):
    """Email configuration for transactional emails via AWS SES.

    In production, these values are provided by CDK via environment variables:
    - EMAIL_ENABLED=true
    - EMAIL_FROM_ADDRESS=noreply@{domain}
    - EMAIL_FROM_NAME=Stardag
    - EMAIL_SES_REGION={aws-region}
    - EMAIL_APP_URL=https://{ui-domain}

    Local development defaults assume email is disabled.
    """

    # Enable/disable email sending (disabled by default, CDK sets to true when SES configured)
    enabled: bool = False
    # From address (must be verified in SES) - set by CDK to noreply@{domain}
    from_address: str = "noreply@localhost"
    # From display name
    from_name: str = "Stardag"
    # AWS SES region
    ses_region: str = "us-east-1"
    # App URL for links in emails - set by CDK to https://{ui-domain}
    app_url: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_prefix="EMAIL_")


class AuthSettings(BaseSettings):
    """Top-level authentication mode configuration.

    - "oidc" (default): users authenticate against an external OIDC provider
      and exchange the OIDC token for an internal token via /auth/exchange.
    - "local": users authenticate with email/password managed by this API,
      which mints internal tokens directly. No external IdP required.
    """

    mode: Literal["oidc", "local"] = "oidc"
    # Allow self-service signup in local mode. Off by default: a self-hosted
    # instance is typically reachable from the public internet.
    local_registration_enabled: bool = False
    # TTL for session tokens minted by local-mode login. Session tokens act
    # as the "refresh" credential (analogous to an OIDC session) and are
    # exchanged for short-lived workspace tokens via /auth/exchange.
    session_token_ttl_hours: int = 24 * 7
    # Bootstrap admin for local mode: created at startup if no user with
    # this email exists (idempotent; an existing user's password is never
    # overwritten). Set both or neither.
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    # Primary workspace bootstrap (local mode only, requires the bootstrap
    # admin): when set, a shared (non-personal) workspace with this name is
    # idempotently created at startup with the bootstrap admin as owner.
    # Typically the name of the Modal/cloud workspace the deployment
    # belongs to, so Stardag mirrors the surrounding platform's structure.
    primary_workspace_name: str | None = None
    # Environment idempotently ensured at startup: in the primary workspace
    # when `primary_workspace_name` is set, otherwise in the bootstrap
    # admin's personal workspace. Set to an empty string to disable.
    primary_workspace_environment: str = "main"

    model_config = SettingsConfigDict(env_prefix="AUTH_")


class OIDCSettings(BaseSettings):
    """OIDC configuration for JWT validation."""

    # Internal issuer URL (for JWKS fetching from within Docker network)
    issuer_url: str = "http://localhost:8080/realms/stardag"
    # External issuer URL (what the browser sees, for token validation)
    external_issuer_url: str | None = None
    # Expected audience claim (comma-separated for multiple audiences)
    audience: str = "stardag-ui,stardag-sdk"
    # JWKS URL (auto-derived from issuer if not set)
    jwks_url: str | None = None
    # Cache JWKS for this many seconds
    jwks_cache_ttl: int = 300
    # OIDC client ID for SDK/CLI authentication
    sdk_client_id: str = "stardag-sdk"
    # OIDC client ID the web UI should use (served via /auth/config so the UI
    # can be configured at runtime instead of at build time)
    ui_client_id: str = "stardag-ui"
    # Cognito hosted-UI domain (only needed for Cognito's non-standard logout;
    # served to the UI via /auth/config)
    cognito_domain: str | None = None

    model_config = SettingsConfigDict(env_prefix="OIDC_")

    @property
    def allowed_audiences(self) -> list[str]:
        """Get list of allowed audience values for token validation."""
        return [a.strip() for a in self.audience.split(",") if a.strip()]

    @property
    def effective_jwks_url(self) -> str:
        """Get JWKS URL, deriving from issuer if not explicitly set."""
        if self.jwks_url:
            return self.jwks_url
        return f"{self.issuer_url}/protocol/openid-connect/certs"

    @property
    def allowed_issuers(self) -> list[str]:
        """Get list of allowed issuer values for token validation."""
        issuers = [self.issuer_url]
        if self.external_issuer_url and self.external_issuer_url != self.issuer_url:
            issuers.append(self.external_issuer_url)
        return issuers

    @property
    def client_issuer_url(self) -> str:
        """Get the issuer URL that clients (SDK/CLI) should use.

        Returns external_issuer_url if set, otherwise issuer_url.
        """
        return self.external_issuer_url or self.issuer_url


settings = Settings()
reaper_settings = ReaperSettings()
claim_settings = ClaimSettings()
jwt_settings = JWTSettings()
auth_settings = AuthSettings()
oidc_settings = OIDCSettings()
email_settings = EmailSettings()
limits_settings = LimitsSettings()
