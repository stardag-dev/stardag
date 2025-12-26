"""Internal JWT token management for org-scoped access tokens.

This module handles:
- RSA key management for signing/verification
- Minting org-scoped access tokens
- Validating internal tokens
- JWKS generation for public key distribution
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from stardag_api.config import jwt_settings

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """Base exception for token operations."""

    pass


class TokenExpiredError(TokenError):
    """Token has expired."""

    pass


class TokenInvalidError(TokenError):
    """Token is invalid."""

    pass


@dataclass
class InternalTokenPayload:
    """Payload for internal org-scoped JWT tokens."""

    sub: str  # User ID (internal, not Keycloak external_id)
    org_id: str  # Organization ID (required for internal tokens)
    iss: str  # Issuer (stardag-api)
    aud: str  # Audience (stardag)
    exp: int  # Expiration timestamp
    iat: int  # Issued at timestamp

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InternalTokenPayload":
        """Create InternalTokenPayload from decoded JWT payload."""
        # Validate required claims
        required = ["sub", "org_id", "iss", "aud", "exp", "iat"]
        for claim in required:
            if claim not in payload:
                raise TokenInvalidError(f"Token missing required '{claim}' claim")

        return cls(
            sub=payload["sub"],
            org_id=payload["org_id"],
            iss=payload["iss"],
            aud=payload["aud"],
            exp=payload["exp"],
            iat=payload["iat"],
        )


class InternalTokenManager:
    """Manages internal JWT tokens with RSA signing.

    Handles key generation, token minting, validation, and JWKS generation.
    """

    def __init__(
        self,
        private_key_pem: str | None = None,
        public_key_pem: str | None = None,
        issuer: str = "stardag-api",
        audience: str = "stardag",
        access_token_ttl_minutes: int = 10,
        key_id: str | None = None,
    ):
        self.issuer = issuer
        self.audience = audience
        self.access_token_ttl = timedelta(minutes=access_token_ttl_minutes)

        # Initialize RSA keys
        if private_key_pem:
            self._private_key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None
            )
            if public_key_pem:
                self._public_key = serialization.load_pem_public_key(
                    public_key_pem.encode()
                )
            else:
                # Derive public key from private key
                self._public_key = self._private_key.public_key()
        else:
            # Generate new RSA keypair
            logger.info("No JWT private key configured, generating ephemeral keypair")
            self._private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            self._public_key = self._private_key.public_key()

        # Generate key ID from public key hash if not provided
        if key_id:
            self._key_id = key_id
        else:
            public_key_bytes = self._public_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            self._key_id = hashlib.sha256(public_key_bytes).hexdigest()[:16]

        # Cache PEM representations for jose library
        self._private_key_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        self._public_key_pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    @property
    def key_id(self) -> str:
        """Get the key ID for this token manager."""
        return self._key_id

    def create_access_token(
        self,
        user_id: str,
        org_id: str,
        additional_claims: dict[str, Any] | None = None,
    ) -> str:
        """Create an org-scoped access token.

        Args:
            user_id: Internal user ID (not Keycloak external_id)
            org_id: Organization ID
            additional_claims: Optional additional claims to include

        Returns:
            Signed JWT string
        """
        now = datetime.now(timezone.utc)
        exp = now + self.access_token_ttl

        payload = {
            "sub": user_id,
            "org_id": org_id,
            "iss": self.issuer,
            "aud": self.audience,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }

        if additional_claims:
            payload.update(additional_claims)

        return jwt.encode(
            payload,
            self._private_key_pem,
            algorithm="RS256",
            headers={"kid": self._key_id},
        )

    def validate_token(self, token: str) -> InternalTokenPayload:
        """Validate an internal token and return its payload.

        Args:
            token: JWT string to validate

        Returns:
            InternalTokenPayload with decoded claims

        Raises:
            TokenExpiredError: If token has expired
            TokenInvalidError: If token is invalid
        """
        try:
            payload = jwt.decode(
                token,
                self._public_key_pem,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_exp": True,
                },
            )
            return InternalTokenPayload.from_dict(payload)

        except ExpiredSignatureError as e:
            raise TokenExpiredError("Token has expired") from e
        except JWTClaimsError as e:
            raise TokenInvalidError(f"Invalid token claims: {e}") from e
        except JWTError as e:
            raise TokenInvalidError(f"Invalid token: {e}") from e

    def get_jwks(self) -> dict[str, Any]:
        """Get JWKS (JSON Web Key Set) for public key distribution.

        Returns:
            JWKS dictionary with public key in JWK format
        """
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        import base64

        # Get the public key numbers
        public_numbers: RSAPublicNumbers = self._public_key.public_numbers()  # type: ignore

        def _int_to_base64url(n: int) -> str:
            """Convert integer to base64url-encoded string."""
            byte_length = (n.bit_length() + 7) // 8
            return (
                base64.urlsafe_b64encode(n.to_bytes(byte_length, byteorder="big"))
                .rstrip(b"=")
                .decode()
            )

        jwk = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self._key_id,
            "n": _int_to_base64url(public_numbers.n),
            "e": _int_to_base64url(public_numbers.e),
        }

        return {"keys": [jwk]}


# Global token manager instance (created lazily)
_token_manager: InternalTokenManager | None = None


def get_token_manager() -> InternalTokenManager:
    """Get the global token manager instance."""
    global _token_manager
    if _token_manager is None:
        _token_manager = InternalTokenManager(
            private_key_pem=jwt_settings.private_key,
            public_key_pem=jwt_settings.public_key,
            issuer=jwt_settings.issuer,
            audience=jwt_settings.audience,
            access_token_ttl_minutes=jwt_settings.access_token_ttl_minutes,
            key_id=jwt_settings.key_id,
        )
    return _token_manager


def create_access_token(user_id: str, org_id: str) -> str:
    """Convenience function to create an access token."""
    return get_token_manager().create_access_token(user_id, org_id)


def validate_token(token: str) -> InternalTokenPayload:
    """Convenience function to validate a token."""
    return get_token_manager().validate_token(token)


def get_jwks() -> dict[str, Any]:
    """Convenience function to get JWKS."""
    return get_token_manager().get_jwks()
