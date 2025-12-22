"""JWT validation using JWKS from OIDC provider."""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from stardag_api.config import oidc_settings

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Raised when authentication fails."""

    pass


@dataclass
class TokenPayload:
    """Parsed JWT token payload."""

    sub: str  # Subject (user identifier from IdP)
    email: str | None
    email_verified: bool
    name: str | None
    given_name: str | None
    family_name: str | None
    preferred_username: str | None
    iss: str  # Issuer
    aud: str | list[str]  # Audience
    exp: int  # Expiration time
    iat: int  # Issued at
    raw: dict[str, Any]  # Full payload for debugging

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TokenPayload":
        """Create TokenPayload from decoded JWT payload."""
        return cls(
            sub=payload["sub"],
            email=payload.get("email"),
            email_verified=payload.get("email_verified", False),
            name=payload.get("name"),
            given_name=payload.get("given_name"),
            family_name=payload.get("family_name"),
            preferred_username=payload.get("preferred_username"),
            iss=payload["iss"],
            aud=payload.get("aud", ""),
            exp=payload["exp"],
            iat=payload.get("iat", 0),
            raw=payload,
        )

    @property
    def display_name(self) -> str:
        """Get best available display name."""
        if self.name:
            return self.name
        if self.given_name or self.family_name:
            parts = [p for p in [self.given_name, self.family_name] if p]
            return " ".join(parts)
        if self.preferred_username:
            return self.preferred_username
        if self.email:
            return self.email.split("@")[0]
        return self.sub


class JWTValidator:
    """Validates JWTs using JWKS from OIDC provider.

    Caches JWKS for performance. Thread-safe for use across requests.
    """

    def __init__(
        self,
        jwks_url: str,
        allowed_issuers: list[str],
        audiences: list[str],
        cache_ttl: int = 300,
    ):
        self.jwks_url = jwks_url
        self.allowed_issuers = allowed_issuers
        self.audiences = audiences
        self.cache_ttl = cache_ttl
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0

    async def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch JWKS from the OIDC provider."""
        async with httpx.AsyncClient() as client:
            response = await client.get(self.jwks_url, timeout=10.0)
            response.raise_for_status()
            return response.json()

    async def _get_jwks(self, force_refresh: bool = False) -> dict[str, Any]:
        """Get JWKS, using cache if available and not expired."""
        now = time.time()
        if (
            not force_refresh
            and self._jwks is not None
            and (now - self._jwks_fetched_at) < self.cache_ttl
        ):
            return self._jwks

        logger.debug("Fetching JWKS from %s", self.jwks_url)
        self._jwks = await self._fetch_jwks()
        self._jwks_fetched_at = now
        return self._jwks

    async def validate_token(self, token: str) -> TokenPayload:
        """Validate a JWT and return the payload.

        Args:
            token: The JWT string to validate

        Returns:
            TokenPayload with decoded claims

        Raises:
            AuthenticationError: If token is invalid, expired, or has wrong claims
        """
        try:
            # First, get the unverified header to find the key ID
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            if not kid:
                raise AuthenticationError("Token missing key ID (kid)")

            # Get JWKS
            jwks = await self._get_jwks()

            # Find the matching key
            key = None
            for k in jwks.get("keys", []):
                if k.get("kid") == kid:
                    key = k
                    break

            if key is None:
                # Maybe JWKS was rotated, try refreshing
                jwks = await self._get_jwks(force_refresh=True)
                for k in jwks.get("keys", []):
                    if k.get("kid") == kid:
                        key = k
                        break

            if key is None:
                raise AuthenticationError(f"Unable to find key with ID: {kid}")

            # Decode and validate the token
            # Note: python-jose will validate exp, iat, and signature
            # We handle audience verification manually to support multiple audiences
            payload = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.allowed_issuers,
                options={
                    "verify_aud": False,  # We verify manually below
                    "verify_iss": True,
                    "verify_exp": True,
                },
            )

            # Manually verify audience
            token_aud = payload.get("aud")
            if token_aud:
                # aud can be a string or list
                token_audiences = (
                    [token_aud] if isinstance(token_aud, str) else token_aud
                )
                if not any(aud in self.audiences for aud in token_audiences):
                    raise AuthenticationError(
                        f"Invalid audience. Expected one of {self.audiences}, "
                        f"got {token_audiences}"
                    )

            return TokenPayload.from_dict(payload)

        except ExpiredSignatureError as e:
            raise AuthenticationError("Token has expired") from e
        except JWTClaimsError as e:
            raise AuthenticationError(f"Invalid token claims: {e}") from e
        except JWTError as e:
            raise AuthenticationError(f"Invalid token: {e}") from e
        except httpx.HTTPError as e:
            logger.error("Failed to fetch JWKS: %s", e)
            raise AuthenticationError(
                "Unable to validate token (JWKS fetch failed)"
            ) from e


# Global validator instance (created lazily)
_validator: JWTValidator | None = None


def get_jwt_validator() -> JWTValidator:
    """Get the global JWT validator instance."""
    global _validator
    if _validator is None:
        _validator = JWTValidator(
            jwks_url=oidc_settings.effective_jwks_url,
            allowed_issuers=oidc_settings.allowed_issuers,
            audiences=oidc_settings.allowed_audiences,
            cache_ttl=oidc_settings.jwks_cache_ttl,
        )
    return _validator
