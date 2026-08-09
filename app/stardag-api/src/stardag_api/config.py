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
jwt_settings = JWTSettings()
auth_settings = AuthSettings()
oidc_settings = OIDCSettings()
email_settings = EmailSettings()
limits_settings = LimitsSettings()
