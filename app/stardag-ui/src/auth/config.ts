// OIDC configuration from environment variables (set at build time)
export const OIDC_ISSUER = import.meta.env.VITE_OIDC_ISSUER || "";
export const OIDC_CLIENT_ID = import.meta.env.VITE_OIDC_CLIENT_ID || "stardag-ui";
export const OIDC_REDIRECT_URI =
  import.meta.env.VITE_OIDC_REDIRECT_URI || `${window.location.origin}/callback`;
export const OIDC_POST_LOGOUT_REDIRECT_URI =
  import.meta.env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI || window.location.origin;

// Check if auth is configured
export function isAuthConfigured(): boolean {
  return !!OIDC_ISSUER;
}
