from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://stardag:stardag@localhost:5432/stardag"
    debug: bool = False

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_")


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


settings = Settings()
jwt_settings = JWTSettings()
oidc_settings = OIDCSettings()
