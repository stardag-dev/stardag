// OIDC configuration from environment variables (set at build time)
export const OIDC_ISSUER = import.meta.env.VITE_OIDC_ISSUER || "";
export const OIDC_CLIENT_ID = import.meta.env.VITE_OIDC_CLIENT_ID || "stardag-ui";
export const OIDC_REDIRECT_URI =
  import.meta.env.VITE_OIDC_REDIRECT_URI || `${window.location.origin}/callback`;
export const OIDC_POST_LOGOUT_REDIRECT_URI =
  import.meta.env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI || window.location.origin;

// Cognito-specific configuration
// The Cognito domain is needed for logout since Cognito doesn't follow standard OIDC logout
// Format: {domain-prefix}.auth.{region}.amazoncognito.com
export const COGNITO_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN || "";

// Check if auth is configured
export function isAuthConfigured(): boolean {
  return !!OIDC_ISSUER;
}

// Check if using Amazon Cognito (based on issuer URL pattern)
export function isCognitoIssuer(): boolean {
  return OIDC_ISSUER.includes("cognito-idp");
}

// Get the Cognito logout URL with required parameters
// Cognito requires client_id and logout_uri (not the standard OIDC parameters)
export function getCognitoLogoutUrl(): string | null {
  if (!isCognitoIssuer() || !COGNITO_DOMAIN) {
    return null;
  }

  const params = new URLSearchParams({
    client_id: OIDC_CLIENT_ID,
    logout_uri: OIDC_POST_LOGOUT_REDIRECT_URI,
  });

  return `https://${COGNITO_DOMAIN}/logout?${params.toString()}`;
}
