// Auth configuration, resolved at app boot.
//
// Sources, in order of precedence:
// 1. Build-time environment variables (VITE_OIDC_*) — kept for backwards
//    compatibility with deployments that bake config into the bundle.
// 2. Runtime config fetched from the API (GET /api/v1/auth/config) — allows
//    a prebuilt UI bundle to be pointed at any IdP (or local auth mode)
//    without rebuilding.
//
// initAuthConfig() must complete before getAuthConfig()/getUserManager()
// are used; main.tsx awaits it before rendering the app.
import { API_V1 } from "../api/config";

export type AuthMode = "oidc" | "local" | "disabled";

export interface AuthConfig {
  mode: AuthMode;
  oidcIssuer: string;
  oidcClientId: string;
  oidcRedirectUri: string;
  oidcPostLogoutRedirectUri: string;
  cognitoDomain: string;
  localRegistrationEnabled: boolean;
}

interface RuntimeAuthConfigResponse {
  auth_mode?: "oidc" | "local";
  oidc_issuer?: string | null;
  oidc_ui_client_id?: string | null;
  cognito_domain?: string | null;
  local_registration_enabled?: boolean;
}

const BUILD_TIME = {
  issuer: import.meta.env.VITE_OIDC_ISSUER || "",
  clientId: import.meta.env.VITE_OIDC_CLIENT_ID || "",
  redirectUri: import.meta.env.VITE_OIDC_REDIRECT_URI || "",
  postLogoutRedirectUri: import.meta.env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI || "",
  cognitoDomain: import.meta.env.VITE_COGNITO_DOMAIN || "",
};

let authConfig: AuthConfig | null = null;

function resolveConfig(runtime: RuntimeAuthConfigResponse | null): AuthConfig {
  const issuer = BUILD_TIME.issuer || runtime?.oidc_issuer || "";
  let mode: AuthMode;
  if (runtime?.auth_mode === "local") {
    mode = "local";
  } else if (issuer) {
    mode = "oidc";
  } else {
    mode = "disabled";
  }
  return {
    mode,
    oidcIssuer: issuer,
    oidcClientId: BUILD_TIME.clientId || runtime?.oidc_ui_client_id || "stardag-ui",
    oidcRedirectUri: BUILD_TIME.redirectUri || `${window.location.origin}/callback`,
    oidcPostLogoutRedirectUri:
      BUILD_TIME.postLogoutRedirectUri || window.location.origin,
    cognitoDomain: BUILD_TIME.cognitoDomain || runtime?.cognito_domain || "",
    localRegistrationEnabled: runtime?.local_registration_enabled ?? false,
  };
}

// Fetch runtime auth config from the API and resolve the effective config.
// Falls back to build-time config if the API is unreachable.
export async function initAuthConfig(): Promise<AuthConfig> {
  if (authConfig) return authConfig;
  let runtime: RuntimeAuthConfigResponse | null = null;
  try {
    const response = await fetch(`${API_V1}/auth/config`, {
      signal: AbortSignal.timeout(5000),
    });
    if (response.ok) {
      runtime = (await response.json()) as RuntimeAuthConfigResponse;
    } else {
      console.warn(`[auth] /auth/config returned ${response.status}`);
    }
  } catch (error) {
    console.warn("[auth] Failed to fetch runtime auth config:", error);
  }
  authConfig = resolveConfig(runtime);
  console.log(
    `[auth] Resolved config: mode=${authConfig.mode}, issuer=${authConfig.oidcIssuer}`,
  );
  return authConfig;
}

export function getAuthConfig(): AuthConfig {
  if (!authConfig) {
    throw new Error("Auth config not initialized - call initAuthConfig() first");
  }
  return authConfig;
}

// Check if auth is configured
export function isAuthConfigured(): boolean {
  return getAuthConfig().mode !== "disabled";
}

// Check if using Amazon Cognito (based on issuer URL pattern)
export function isCognitoIssuer(): boolean {
  return getAuthConfig().oidcIssuer.includes("cognito-idp");
}

// Get the Cognito logout URL with required parameters
// Cognito requires client_id and logout_uri (not the standard OIDC parameters)
export function getCognitoLogoutUrl(): string | null {
  const config = getAuthConfig();
  if (!isCognitoIssuer() || !config.cognitoDomain) {
    return null;
  }

  const params = new URLSearchParams({
    client_id: config.oidcClientId,
    logout_uri: config.oidcPostLogoutRedirectUri,
  });

  return `https://${config.cognitoDomain}/logout?${params.toString()}`;
}
