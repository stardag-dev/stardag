from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://stardag:stardag@localhost:5432/stardag"
    debug: bool = False

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_")


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
oidc_settings = OIDCSettings()
