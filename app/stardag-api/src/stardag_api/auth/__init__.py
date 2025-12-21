"""Authentication module for Stardag API."""

from stardag_api.auth.dependencies import (
    ApiKeyAuth,
    SdkAuth,
    get_api_key_auth,
    get_current_user,
    get_current_user_optional,
    get_optional_token,
    get_sdk_auth,
    get_token,
    require_api_key_auth,
    require_sdk_auth,
    verify_workspace_access,
)
from stardag_api.auth.jwt import JWTValidator, TokenPayload

__all__ = [
    "ApiKeyAuth",
    "JWTValidator",
    "SdkAuth",
    "TokenPayload",
    "get_api_key_auth",
    "get_current_user",
    "get_current_user_optional",
    "get_optional_token",
    "get_sdk_auth",
    "get_token",
    "require_api_key_auth",
    "require_sdk_auth",
    "verify_workspace_access",
]
