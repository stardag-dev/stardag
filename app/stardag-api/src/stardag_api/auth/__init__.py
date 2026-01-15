"""Authentication module for Stardag API.

Token types:
- Internal tokens: Org-scoped JWTs minted by /auth/exchange (used by most endpoints)
- OIDC tokens: External JWTs from OIDC provider (for /auth/exchange and bootstrap endpoints)
- API keys: Environment-scoped keys for SDK/automation
"""

from stardag_api.auth.dependencies import (
    ApiKeyAuth,
    SdkAuth,
    get_api_key_auth,
    get_current_user,
    get_current_user_flexible,
    get_current_user_optional,
    get_oidc_token,
    get_optional_token,
    get_or_create_user_from_oidc,
    get_org_id_from_token,
    get_sdk_auth,
    get_token,
    require_api_key_auth,
    require_sdk_auth,
    verify_environment_access,
)
from stardag_api.auth.jwt import JWTValidator, TokenPayload as OIDCTokenPayload
from stardag_api.auth.tokens import (
    InternalTokenPayload,
    InternalTokenManager,
    TokenError,
    TokenExpiredError,
    TokenInvalidError,
    create_access_token,
    get_jwks,
    get_token_manager,
    validate_token,
)

__all__ = [
    # Dependencies
    "ApiKeyAuth",
    "SdkAuth",
    "get_api_key_auth",
    "get_current_user",
    "get_current_user_flexible",
    "get_current_user_optional",
    "get_oidc_token",
    "get_optional_token",
    "get_or_create_user_from_oidc",
    "get_org_id_from_token",
    "get_sdk_auth",
    "get_token",
    "require_api_key_auth",
    "require_sdk_auth",
    "verify_environment_access",
    # OIDC JWT validation (for /auth/exchange and bootstrap endpoints)
    "JWTValidator",
    "OIDCTokenPayload",
    # Internal tokens
    "InternalTokenPayload",
    "InternalTokenManager",
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "create_access_token",
    "get_jwks",
    "get_token_manager",
    "validate_token",
]
