from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database configuration - can use either database_url or individual params
    database_url: str | None = None
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "stardag"
    database_user: str = "stardag"
    database_password: str = "stardag"

    debug: bool = False

    # CORS origins (comma-separated)
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

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
oidc_settings = OIDCSettings()
