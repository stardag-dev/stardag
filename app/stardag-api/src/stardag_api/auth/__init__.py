"""Authentication module for Stardag API.

Token types:
- Internal tokens: Org-scoped JWTs minted by /auth/exchange (used by most endpoints)
- Keycloak tokens: External JWTs from OIDC provider (only /auth/exchange)
- API keys: Workspace-scoped keys for SDK/automation
"""

from stardag_api.auth.dependencies import (
    ApiKeyAuth,
    SdkAuth,
    get_api_key_auth,
    get_current_user,
    get_current_user_optional,
    get_optional_token,
    get_org_id_from_token,
    get_sdk_auth,
    get_token,
    require_api_key_auth,
    require_sdk_auth,
    verify_workspace_access,
)
from stardag_api.auth.jwt import JWTValidator, TokenPayload as KeycloakTokenPayload
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
    "get_current_user_optional",
    "get_optional_token",
    "get_org_id_from_token",
    "get_sdk_auth",
    "get_token",
    "require_api_key_auth",
    "require_sdk_auth",
    "verify_workspace_access",
    # Keycloak JWT validation (only for /auth/exchange)
    "JWTValidator",
    "KeycloakTokenPayload",
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
